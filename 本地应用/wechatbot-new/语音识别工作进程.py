"""在独立媒体环境中解码并识别；真实语音只通过stdout管道返回父进程。"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import wave


def infer(source, model_path, temporary):
    from faster_whisper import WhisperModel
    source = Path(source)
    # 此目录由父进程持有；子进程被超时终止后父进程仍执行清理。
    with contextlib.nullcontext(temporary) as directory:
        if source.suffix.lower() == '.silk':
            import pilk
            pcm = Path(directory) / 'decoded.pcm'
            wav = Path(directory) / 'decoded.wav'
            pilk.decode(str(source), str(pcm), pcm_rate=16000)
            if not pcm.exists() or not pcm.stat().st_size:
                raise ValueError('EmptyDecodedAudio')
            with wave.open(str(wav), 'wb') as target:
                target.setparams((1, 2, 16000, 0, 'NONE', 'NONE'))
                target.writeframes(pcm.read_bytes())
            source = wav
        # 先限长；解码使用PyAV，无需系统ffmpeg。最多3分钟。
        from faster_whisper.audio import decode_audio
        audio = decode_audio(str(source), sampling_rate=16000)
        duration = len(audio) / 16000
        if not 0 < duration <= 180:
            raise ValueError('AudioDurationLimit')
        model = WhisperModel(str(model_path), device='cpu', compute_type='int8',
                             cpu_threads=min(os.cpu_count() or 4, 6), num_workers=1,
                             local_files_only=True)
        segments, info = model.transcribe(audio, language='zh', beam_size=5,
                                         vad_filter=True, condition_on_previous_text=False)
        text = ''.join(segment.text for segment in segments).strip()
        return dict(ok=bool(text), text=text, language=info.language, duration_seconds=round(duration, 2),
                    error='' if text else 'NoSpeechRecognized')


if __name__ == '__main__':
    try:
        # 隔离第三方Python日志；原生解码器输出不落盘，父进程也不会保存stdout原文。
        with contextlib.redirect_stderr(io.StringIO()):
            value = infer(sys.argv[1], sys.argv[2], sys.argv[3])
    except Exception as error:
        value = {'ok': False, 'error': type(error).__name__}
    print(json.dumps(value, ensure_ascii=False), flush=True)
