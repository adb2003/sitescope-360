/**
 * Expo implementation of {@link CameraTransport}.
 *
 * Separated from `osc.ts` so the protocol can be tested under plain Node with no Expo, no Metro
 * and no device. This file is the part that genuinely cannot be tested without a phone, so it is
 * kept as thin as possible — it wires `fetch` and `expo-file-system` to the interface and does
 * nothing else.
 */

import * as FileSystem from 'expo-file-system'

import { CameraError, type CameraTransport } from './osc'

export const CAMERA_DOWNLOAD_DIR = `${FileSystem.documentDirectory}camera/`

async function ensureDownloadDir(): Promise<void> {
  const info = await FileSystem.getInfoAsync(CAMERA_DOWNLOAD_DIR)
  if (!info.exists) {
    await FileSystem.makeDirectoryAsync(CAMERA_DOWNLOAD_DIR, { intermediates: true })
  }
}

export const expoCameraTransport: CameraTransport = {
  fetch: globalThis.fetch as CameraTransport['fetch'],

  async downloadFile(url, destination, onProgress) {
    await ensureDownloadDir()

    // createDownloadResumable rather than downloadAsync: a 360 video is hundreds of megabytes
    // over a camera's Wi-Fi, so the operator needs to see it moving. A silent five-minute wait
    // is indistinguishable from a hang, and the usual response to a hang is to kill the app --
    // which is exactly when the capture gets lost.
    const resumable = FileSystem.createDownloadResumable(
      url,
      destination,
      {},
      onProgress
        ? ({ totalBytesWritten, totalBytesExpectedToWrite }) => {
            const fraction =
              totalBytesExpectedToWrite > 0
                ? totalBytesWritten / totalBytesExpectedToWrite
                : 0
            onProgress(fraction, totalBytesWritten)
          }
        : undefined,
    )

    const result = await resumable.downloadAsync().catch((error: unknown) => {
      throw new CameraError(
        'The transfer from the camera failed. Stay on the camera’s Wi-Fi and try again — the recording is still on the camera.',
        'unreachable',
        error,
      )
    })

    if (!result?.uri) {
      // Reassurance is the point: nothing has been lost, and the operator should not re-shoot.
      throw new CameraError(
        'The transfer ended without producing a file. The recording is still on the camera, so it can be retried.',
        'protocol',
      )
    }
    return result.uri
  },
}
