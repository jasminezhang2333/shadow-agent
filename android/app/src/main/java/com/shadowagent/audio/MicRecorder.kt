package com.shadowagent.audio

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import kotlin.concurrent.thread

/**
 * 麦克风采集：PCM 16kHz / 16bit / mono，和 realtime-MLLM 要求一致（INTERFACE.md §2）。
 * 每 100ms（3200 字节）回调一帧，交给 uploader 推给后端。
 *
 * 用普通 MIC source，不碰用户播放/通话音频，纯被动听用户说话以触发回忆/比价意图。
 */
class MicRecorder(private val onPcm: (ByteArray) -> Unit) {

    private val sampleRate = 16000
    private val frameBytes = 3200 // 100ms @ 16k/16bit/mono
    private var recorder: AudioRecord? = null
    @Volatile private var running = false

    @SuppressLint("MissingPermission")
    fun start() {
        val minBuf = AudioRecord.getMinBufferSize(
            sampleRate,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT
        )
        recorder = AudioRecord(
            MediaRecorder.AudioSource.MIC,
            sampleRate,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            maxOf(minBuf, frameBytes * 4)
        )
        recorder?.startRecording()
        running = true
        thread(name = "mic-recorder") {
            val buf = ByteArray(frameBytes)
            while (running) {
                val n = recorder?.read(buf, 0, buf.size) ?: 0
                if (n > 0) onPcm(buf.copyOf(n))
            }
        }
    }

    fun stop() {
        running = false
        recorder?.stop()
        recorder?.release()
        recorder = null
    }
}
