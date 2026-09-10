/**
 * Equirectangular 360 viewer built directly on Three.js (§11).
 *
 * Why Three.js rather than Pannellum or Marzipano: this viewer has to do more than show a
 * panorama. It projects annotation pins and neighbour hotspots into screen space, synchronises
 * its orientation with a second viewer for date comparison, and reports the yaw of a click so a
 * finding can be pinned. All three need the camera matrix, which a wrapped viewer hides. Driving
 * the scene ourselves is ~200 lines and removes a dependency whose API we would be fighting.
 *
 * Implementation notes that matter:
 *
 * - The panorama is the inside of a sphere (`side: BackSide`), not a negatively-scaled mesh:
 *   flipping scale also flips face winding and breaks raycasting.
 * - Drag sensitivity scales with the field of view, so a zoomed-in drag moves proportionally
 *   less. Fixed sensitivity feels broken at 25° FOV.
 * - Textures are cached and reference-counted across viewer instances, because the compare view
 *   mounts two viewers over the same images and the timeline strip re-mounts constantly.
 *   Uncached, a session of clicking through 20 frames would allocate 20 full panoramas of GPU
 *   memory and never release them.
 */

import { useCallback, useEffect, useImperativeHandle, useRef, useState, forwardRef } from 'react'
import * as THREE from 'three'

import { clamp } from '@/lib/utils'

export interface Orientation {
  /** Degrees, 0 = the panorama's forward direction, increasing to the right. */
  yaw: number
  /** Degrees, positive up. Clamped to ±85 to avoid gimbal flip at the poles. */
  pitch: number
  /** Vertical field of view in degrees; smaller is more zoomed in. */
  fov: number
}

export interface PanoramaHandle {
  getOrientation: () => Orientation
  setOrientation: (orientation: Partial<Orientation>) => void
  /** Screen position of a spherical direction, or null when it is behind the camera. */
  project: (yaw: number, pitch: number) => { x: number; y: number } | null
  requestFullscreen: () => void
  resetView: () => void
}

export interface PanoramaCanvasProps {
  /** Full-resolution image URL. */
  src: string
  /** Optional lower-resolution URL shown first so the view is usable while the full loads. */
  previewSrc?: string | null
  initialYaw?: number
  className?: string
  /** Emitted on every frame the orientation changes — used to synchronise a paired viewer. */
  onOrientationChange?: (orientation: Orientation) => void
  /** Emitted when the user clicks the panorama, with the spherical direction clicked. */
  onPick?: (yaw: number, pitch: number, event: PointerEvent) => void
  onLoadStateChange?: (state: 'loading' | 'preview' | 'ready' | 'error') => void
  /** Disables interaction — used by the follower in a synchronised pair. */
  interactive?: boolean
}

const MIN_FOV = 22
const MAX_FOV = 100
const DEFAULT_FOV = 74
const MAX_PITCH = 85
const SPHERE_RADIUS = 500

// ---- texture cache ----------------------------------------------------------
interface CacheEntry {
  texture: THREE.Texture
  refs: number
  lastUsed: number
}

const textureCache = new Map<string, CacheEntry>()
/** Roughly 8 × 8k panoramas — enough for a filmstrip session, bounded enough for a tablet. */
const MAX_CACHED_TEXTURES = 8
const loader = new THREE.TextureLoader()
loader.setCrossOrigin('anonymous')

function acquireTexture(url: string): Promise<THREE.Texture> {
  const cached = textureCache.get(url)
  if (cached) {
    cached.refs += 1
    cached.lastUsed = Date.now()
    return Promise.resolve(cached.texture)
  }

  return new Promise((resolve, reject) => {
    loader.load(
      url,
      (texture) => {
        texture.colorSpace = THREE.SRGBColorSpace
        // The seam is a wrap, so the horizontal axis must repeat rather than clamp — otherwise a
        // visible line appears at longitude ±180°.
        texture.wrapS = THREE.RepeatWrapping
        texture.wrapT = THREE.ClampToEdgeWrapping
        texture.minFilter = THREE.LinearFilter
        texture.generateMipmaps = false
        textureCache.set(url, { texture, refs: 1, lastUsed: Date.now() })
        evictTextures()
        resolve(texture)
      },
      undefined,
      () => reject(new Error(`Could not load panorama: ${url}`)),
    )
  })
}

function releaseTexture(url: string) {
  const entry = textureCache.get(url)
  if (!entry) return
  entry.refs = Math.max(0, entry.refs - 1)
  entry.lastUsed = Date.now()
  evictTextures()
}

function evictTextures() {
  if (textureCache.size <= MAX_CACHED_TEXTURES) return
  const unused = [...textureCache.entries()]
    .filter(([, entry]) => entry.refs === 0)
    .sort((a, b) => a[1].lastUsed - b[1].lastUsed)

  let excess = textureCache.size - MAX_CACHED_TEXTURES
  for (const [url, entry] of unused) {
    if (excess <= 0) break
    entry.texture.dispose()
    textureCache.delete(url)
    excess -= 1
  }
}

/** Exposed for tests and for a hard reset when the user signs out. */
export function clearPanoramaCache() {
  for (const [, entry] of textureCache) entry.texture.dispose()
  textureCache.clear()
}

// ---- component --------------------------------------------------------------
export const PanoramaCanvas = forwardRef<PanoramaHandle, PanoramaCanvasProps>(
  function PanoramaCanvas(
    {
      src,
      previewSrc,
      initialYaw = 0,
      className,
      onOrientationChange,
      onPick,
      onLoadStateChange,
      interactive = true,
    },
    ref,
  ) {
    const mountRef = useRef<HTMLDivElement>(null)
    const [error, setError] = useState<string | null>(null)

    // Mutable render state, kept in refs: these change every animation frame and must not
    // trigger React renders.
    const orientation = useRef<Orientation>({ yaw: initialYaw, pitch: 0, fov: DEFAULT_FOV })
    const rendererRef = useRef<THREE.WebGLRenderer | null>(null)
    const cameraRef = useRef<THREE.PerspectiveCamera | null>(null)
    const sceneRef = useRef<THREE.Scene | null>(null)
    const meshRef = useRef<THREE.Mesh | null>(null)
    const needsRender = useRef(true)
    const heldUrls = useRef<Set<string>>(new Set())
    const changeCallback = useRef(onOrientationChange)
    changeCallback.current = onOrientationChange
    const pickCallback = useRef(onPick)
    pickCallback.current = onPick
    const loadStateCallback = useRef(onLoadStateChange)
    loadStateCallback.current = onLoadStateChange

    // ---- scene setup ------------------------------------------------------
    useEffect(() => {
      const mount = mountRef.current
      if (!mount) return

      // Captured now rather than read in cleanup: the linter is right that a ref can change
      // between effect setup and teardown, and releasing the wrong set would leak GPU textures.
      const heldForThisMount = heldUrls.current

      const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false })
      renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
      renderer.setSize(mount.clientWidth || 1, mount.clientHeight || 1)
      mount.appendChild(renderer.domElement)
      renderer.domElement.style.display = 'block'
      renderer.domElement.style.touchAction = 'none'
      renderer.domElement.setAttribute('aria-label', '360 degree panorama')

      const scene = new THREE.Scene()
      const camera = new THREE.PerspectiveCamera(
        DEFAULT_FOV,
        (mount.clientWidth || 1) / (mount.clientHeight || 1),
        0.1,
        SPHERE_RADIUS * 2.2,
      )

      const geometry = new THREE.SphereGeometry(SPHERE_RADIUS, 72, 44)
      const material = new THREE.MeshBasicMaterial({ side: THREE.BackSide })
      const mesh = new THREE.Mesh(geometry, material)
      scene.add(mesh)

      rendererRef.current = renderer
      cameraRef.current = camera
      sceneRef.current = scene
      meshRef.current = mesh
      needsRender.current = true

      // Render on demand rather than every frame: a static panorama at 60fps burns battery on a
      // site tablet for no visual benefit.
      let raf = 0
      const tick = () => {
        raf = requestAnimationFrame(tick)
        if (!needsRender.current) return
        needsRender.current = false
        applyOrientation(camera, orientation.current)
        renderer.render(scene, camera)
      }
      raf = requestAnimationFrame(tick)

      const observer = new ResizeObserver(() => {
        const width = mount.clientWidth || 1
        const height = mount.clientHeight || 1
        renderer.setSize(width, height)
        camera.aspect = width / height
        camera.updateProjectionMatrix()
        needsRender.current = true
      })
      observer.observe(mount)

      return () => {
        cancelAnimationFrame(raf)
        observer.disconnect()
        geometry.dispose()
        material.dispose()
        renderer.dispose()
        if (renderer.domElement.parentNode === mount) {
          mount.removeChild(renderer.domElement)
        }
        for (const url of heldForThisMount) releaseTexture(url)
        heldForThisMount.clear()
        rendererRef.current = null
        cameraRef.current = null
        sceneRef.current = null
        meshRef.current = null
      }
    }, [])

    // ---- texture loading --------------------------------------------------
    useEffect(() => {
      let cancelled = false
      const held = heldUrls.current
      setError(null)
      loadStateCallback.current?.('loading')

      const assign = (texture: THREE.Texture) => {
        const mesh = meshRef.current
        if (!mesh || cancelled) return
        const material = mesh.material as THREE.MeshBasicMaterial
        material.map = texture
        material.needsUpdate = true
        needsRender.current = true
      }

      // Show the preview first when there is one: a 1024px image decodes in a fraction of the
      // time, so the operator can start looking around while the full resolution arrives.
      const previewPromise = previewSrc
        ? acquireTexture(previewSrc)
            .then((texture) => {
              if (cancelled) {
                releaseTexture(previewSrc)
                return
              }
              held.add(previewSrc)
              assign(texture)
              loadStateCallback.current?.('preview')
            })
            .catch(() => undefined)
        : Promise.resolve()

      void previewPromise.then(() =>
        acquireTexture(src)
          .then((texture) => {
            if (cancelled) {
              releaseTexture(src)
              return
            }
            held.add(src)
            assign(texture)
            loadStateCallback.current?.('ready')
          })
          .catch((caught: Error) => {
            if (cancelled) return
            setError(caught.message)
            loadStateCallback.current?.('error')
          }),
      )

      return () => {
        cancelled = true
        for (const url of [src, previewSrc]) {
          if (url && held.has(url)) {
            releaseTexture(url)
            held.delete(url)
          }
        }
      }
    }, [src, previewSrc])

    // Re-centre when the caller changes the requested opening yaw (e.g. navigating frames).
    useEffect(() => {
      orientation.current.yaw = initialYaw
      orientation.current.pitch = 0
      needsRender.current = true
    }, [initialYaw])

    // ---- interaction ------------------------------------------------------
    const emit = useCallback(() => {
      needsRender.current = true
      changeCallback.current?.({ ...orientation.current })
    }, [])

    useEffect(() => {
      const renderer = rendererRef.current
      const element = renderer?.domElement
      if (!element || !interactive) return

      let dragging = false
      let moved = 0
      let lastX = 0
      let lastY = 0
      let pointerId: number | null = null

      const onPointerDown = (event: PointerEvent) => {
        if (event.button !== 0 && event.pointerType === 'mouse') return
        dragging = true
        moved = 0
        lastX = event.clientX
        lastY = event.clientY
        pointerId = event.pointerId
        element.setPointerCapture(event.pointerId)
        element.style.cursor = 'grabbing'
      }

      const onPointerMove = (event: PointerEvent) => {
        if (!dragging || event.pointerId !== pointerId) return
        const dx = event.clientX - lastX
        const dy = event.clientY - lastY
        lastX = event.clientX
        lastY = event.clientY
        moved += Math.abs(dx) + Math.abs(dy)

        // Sensitivity proportional to FOV: dragging 100px should sweep the same *fraction* of
        // the view whether zoomed in or out.
        const scale = orientation.current.fov / (element.clientHeight || 1)
        orientation.current.yaw -= dx * scale
        orientation.current.pitch = clamp(
          orientation.current.pitch + dy * scale,
          -MAX_PITCH,
          MAX_PITCH,
        )
        emit()
      }

      const onPointerUp = (event: PointerEvent) => {
        if (event.pointerId !== pointerId) return
        dragging = false
        pointerId = null
        element.style.cursor = 'grab'
        if (element.hasPointerCapture(event.pointerId)) {
          element.releasePointerCapture(event.pointerId)
        }
        // A click is a press that barely moved. Without this threshold every drag would also
        // drop an annotation pin.
        if (moved < 6 && pickCallback.current) {
          const picked = pickDirection(event, element, cameraRef.current)
          if (picked) pickCallback.current(picked.yaw, picked.pitch, event)
        }
      }

      const onWheel = (event: WheelEvent) => {
        event.preventDefault()
        orientation.current.fov = clamp(
          orientation.current.fov + Math.sign(event.deltaY) * 4,
          MIN_FOV,
          MAX_FOV,
        )
        emit()
      }

      const onKeyDown = (event: KeyboardEvent) => {
        const step = event.shiftKey ? 15 : 5
        switch (event.key) {
          case 'ArrowLeft':
            orientation.current.yaw -= step
            break
          case 'ArrowRight':
            orientation.current.yaw += step
            break
          case 'ArrowUp':
            orientation.current.pitch = clamp(orientation.current.pitch + step, -MAX_PITCH, MAX_PITCH)
            break
          case 'ArrowDown':
            orientation.current.pitch = clamp(orientation.current.pitch - step, -MAX_PITCH, MAX_PITCH)
            break
          case '+':
          case '=':
            orientation.current.fov = clamp(orientation.current.fov - 5, MIN_FOV, MAX_FOV)
            break
          case '-':
            orientation.current.fov = clamp(orientation.current.fov + 5, MIN_FOV, MAX_FOV)
            break
          default:
            return
        }
        event.preventDefault()
        emit()
      }

      // Two-finger pinch to zoom on touch devices.
      let pinchStart: { distance: number; fov: number } | null = null
      const activeTouches = new Map<number, { x: number; y: number }>()

      const onTouchPointerDown = (event: PointerEvent) => {
        if (event.pointerType !== 'touch') return
        activeTouches.set(event.pointerId, { x: event.clientX, y: event.clientY })
        if (activeTouches.size === 2) {
          const [first, second] = [...activeTouches.values()]
          if (first && second) {
            pinchStart = {
              distance: Math.hypot(second.x - first.x, second.y - first.y),
              fov: orientation.current.fov,
            }
            dragging = false
          }
        }
      }
      const onTouchPointerMove = (event: PointerEvent) => {
        if (event.pointerType !== 'touch' || !activeTouches.has(event.pointerId)) return
        activeTouches.set(event.pointerId, { x: event.clientX, y: event.clientY })
        if (pinchStart && activeTouches.size === 2) {
          const [first, second] = [...activeTouches.values()]
          if (first && second) {
            const distance = Math.hypot(second.x - first.x, second.y - first.y)
            const ratio = pinchStart.distance / Math.max(distance, 1)
            orientation.current.fov = clamp(pinchStart.fov * ratio, MIN_FOV, MAX_FOV)
            emit()
          }
        }
      }
      const onTouchPointerUp = (event: PointerEvent) => {
        activeTouches.delete(event.pointerId)
        if (activeTouches.size < 2) pinchStart = null
      }

      element.style.cursor = 'grab'
      element.tabIndex = 0
      element.addEventListener('pointerdown', onPointerDown)
      element.addEventListener('pointerdown', onTouchPointerDown)
      element.addEventListener('pointermove', onPointerMove)
      element.addEventListener('pointermove', onTouchPointerMove)
      element.addEventListener('pointerup', onPointerUp)
      element.addEventListener('pointerup', onTouchPointerUp)
      element.addEventListener('pointercancel', onPointerUp)
      element.addEventListener('pointercancel', onTouchPointerUp)
      element.addEventListener('wheel', onWheel, { passive: false })
      element.addEventListener('keydown', onKeyDown)

      return () => {
        element.removeEventListener('pointerdown', onPointerDown)
        element.removeEventListener('pointerdown', onTouchPointerDown)
        element.removeEventListener('pointermove', onPointerMove)
        element.removeEventListener('pointermove', onTouchPointerMove)
        element.removeEventListener('pointerup', onPointerUp)
        element.removeEventListener('pointerup', onTouchPointerUp)
        element.removeEventListener('pointercancel', onPointerUp)
        element.removeEventListener('pointercancel', onTouchPointerUp)
        element.removeEventListener('wheel', onWheel)
        element.removeEventListener('keydown', onKeyDown)
      }
    }, [interactive, emit])

    // ---- imperative handle ------------------------------------------------
    useImperativeHandle(
      ref,
      () => ({
        getOrientation: () => ({ ...orientation.current }),
        setOrientation: (next) => {
          if (next.yaw !== undefined) orientation.current.yaw = next.yaw
          if (next.pitch !== undefined) {
            orientation.current.pitch = clamp(next.pitch, -MAX_PITCH, MAX_PITCH)
          }
          if (next.fov !== undefined) {
            orientation.current.fov = clamp(next.fov, MIN_FOV, MAX_FOV)
          }
          needsRender.current = true
        },
        project: (yaw, pitch) => {
          const camera = cameraRef.current
          const renderer = rendererRef.current
          if (!camera || !renderer) return null
          applyOrientation(camera, orientation.current)
          const point = sphericalToVector(yaw, pitch).multiplyScalar(SPHERE_RADIUS * 0.9)
          const projected = point.clone().project(camera)
          // z outside [-1, 1] means behind the camera or beyond the far plane.
          if (projected.z < -1 || projected.z > 1) return null
          const size = renderer.getSize(new THREE.Vector2())
          return {
            x: ((projected.x + 1) / 2) * size.x,
            y: ((1 - projected.y) / 2) * size.y,
          }
        },
        requestFullscreen: () => {
          const mount = mountRef.current?.parentElement ?? mountRef.current
          if (!mount) return
          if (document.fullscreenElement) {
            void document.exitFullscreen()
          } else {
            void mount.requestFullscreen?.()
          }
        },
        resetView: () => {
          orientation.current = { yaw: initialYaw, pitch: 0, fov: DEFAULT_FOV }
          emit()
        },
      }),
      [initialYaw, emit],
    )

    return (
      <div className={className} ref={mountRef}>
        {error ? (
          <div className="flex h-full w-full items-center justify-center bg-canvas p-6 text-center text-sm text-bad">
            {error}
          </div>
        ) : null}
      </div>
    )
  },
)

// ---- geometry helpers -------------------------------------------------------
/**
 * Spherical direction → unit vector, matching the backend's convention exactly
 * (`app/vision/panorama.py`): longitude increases to the right, +Z is the image centre, +Y up.
 * Any mismatch here would put annotations and hotspots in the wrong place.
 */
export function sphericalToVector(yawDeg: number, pitchDeg: number): THREE.Vector3 {
  const yaw = THREE.MathUtils.degToRad(yawDeg)
  const pitch = THREE.MathUtils.degToRad(pitchDeg)
  const cosPitch = Math.cos(pitch)
  return new THREE.Vector3(cosPitch * Math.sin(yaw), Math.sin(pitch), cosPitch * Math.cos(yaw))
}

export function vectorToSpherical(vector: THREE.Vector3): { yaw: number; pitch: number } {
  const unit = vector.clone().normalize()
  return {
    yaw: THREE.MathUtils.radToDeg(Math.atan2(unit.x, unit.z)),
    pitch: THREE.MathUtils.radToDeg(Math.asin(clamp(unit.y, -1, 1))),
  }
}

function applyOrientation(camera: THREE.PerspectiveCamera, orientation: Orientation) {
  if (camera.fov !== orientation.fov) {
    camera.fov = orientation.fov
    camera.updateProjectionMatrix()
  }
  const target = sphericalToVector(orientation.yaw, orientation.pitch).multiplyScalar(
    SPHERE_RADIUS,
  )
  camera.position.set(0, 0, 0)
  camera.lookAt(target)
  camera.updateMatrixWorld()
}

function pickDirection(
  event: PointerEvent,
  element: HTMLElement,
  camera: THREE.PerspectiveCamera | null,
): { yaw: number; pitch: number } | null {
  if (!camera) return null
  const rect = element.getBoundingClientRect()
  const ndc = new THREE.Vector2(
    ((event.clientX - rect.left) / rect.width) * 2 - 1,
    -(((event.clientY - rect.top) / rect.height) * 2 - 1),
  )
  // The ray direction from the camera *is* the spherical direction, since the camera sits at the
  // sphere's centre — no intersection test needed.
  const raycaster = new THREE.Raycaster()
  raycaster.setFromCamera(ndc, camera)
  return vectorToSpherical(raycaster.ray.direction)
}
