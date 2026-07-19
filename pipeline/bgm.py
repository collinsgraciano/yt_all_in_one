"""运行核心：BGM 混音（numpy/scipy 信号处理）。

对应原 runtime_core.py 行 3274-3599：
- load_music_segment_cached
- analyze_audio / compute_volume_envelope / analyze_spectral_gaps
- apply_highpass_filter / apply_spectral_shaping
- apply_dynamic_volume / apply_stereo_offset
- get_all_music_files
- prepare_copyright_music
- mix_with_bgm
"""

from __future__ import annotations

import glob
import math
import os
import random
import time
from functools import lru_cache

import numpy as np
from scipy.signal import butter, sosfilt, stft, istft
from pydub import AudioSegment

from . import config as cfg
from .runtime import log


# ============================================================================
# 缓存 BGM 源文件以降低长批处理中重复解码开销（原文件行 3284-3287）
# ============================================================================
@lru_cache(maxsize=4)
def load_music_segment_cached(music_path):
    """缓存少量 BGM 源文件，减少长批处理中重复解码的开销。

    在低配 VPS 上将缓存从 8 降到 4，每首 BGM 约 3-5MB，
    节省 ~20MB 常驻内存。
    """
    return AudioSegment.from_file(music_path)


# ============================================================================
# 音频分析（原文件行 3290-3330）
# ============================================================================
def analyze_audio(audio_segment):
    duration_ms = len(audio_segment)
    rms_dbfs = audio_segment.dBFS
    peak_dbfs = audio_segment.max_dBFS

    chunk_size_ms = 500
    chunks = [
        audio_segment[i : i + chunk_size_ms]
        for i in range(0, duration_ms, chunk_size_ms)
        if i + chunk_size_ms <= duration_ms
    ]

    chunk_levels = []
    for chunk in chunks:
        try:
            level = chunk.dBFS
            if level > -60:
                chunk_levels.append(level)
        except Exception:
            pass
    dynamic_range_db = (max(chunk_levels) - min(chunk_levels)) if len(chunk_levels) >= 2 else 0
    return {
        "rms_dbfs": rms_dbfs,
        "peak_dbfs": peak_dbfs,
        "dynamic_range_db": dynamic_range_db,
        "duration_ms": duration_ms,
        "sample_rate": audio_segment.frame_rate,
        "channels": audio_segment.channels,
    }


def compute_volume_envelope(audio_segment, window_ms=200):
    duration_ms = len(audio_segment)
    envelope = []
    for i in range(0, duration_ms, window_ms):
        chunk = audio_segment[i : i + window_ms]
        if len(chunk) < 50:
            envelope.append(envelope[-1] if envelope else -60)
            continue
        try:
            level = max(chunk.dBFS, -60)
            envelope.append(level)
        except Exception:
            envelope.append(-60)
    return np.array(envelope), window_ms


def analyze_spectral_gaps(audio_segment, n_bands=8):
    sample_rate = audio_segment.frame_rate

    # ── 超长音频（>10 分钟）仅分析前 5 分钟，避免全局 STFT 内存爆炸 ──
    # 频谱带增益用于 BGM 塑形，取开头 5 分钟已具代表性，峰值内存从 ~2GB 降至 ~10MB
    _MAX_ANALYSIS_MS = 5 * 60 * 1000
    if len(audio_segment) > _MAX_ANALYSIS_MS:
        audio_segment = audio_segment[:_MAX_ANALYSIS_MS]

    samples = np.array(audio_segment.get_array_of_samples(), dtype=np.float64)
    if audio_segment.channels > 1:
        samples = samples.reshape((-1, audio_segment.channels)).mean(axis=1)

    max_val = 2 ** (audio_segment.sample_width * 8 - 1)
    samples = samples / max_val
    nperseg = min(4096, len(samples))
    freqs, times, Zxx = stft(samples, fs=sample_rate, nperseg=nperseg)
    power = np.abs(Zxx) ** 2

    nyquist = sample_rate / 2
    max_freq = min(nyquist, 16000)
    band_edges = np.logspace(np.log10(150), np.log10(max_freq), n_bands + 1)

    band_energies = []
    for i in range(n_bands):
        mask = (freqs >= band_edges[i]) & (freqs < band_edges[i + 1])
        band_energies.append(power[mask].mean() if mask.any() else 1e-10)

    band_energies_db = 10 * np.log10(np.array(band_energies) + 1e-10)
    max_energy_db = band_energies_db.max()
    relative_db = band_energies_db - max_energy_db
    band_gains = np.clip(-relative_db * 0.3, 0, 6)
    return band_gains, band_edges


# ============================================================================
# 信号处理（原文件行 3360-3481）
# ============================================================================
def apply_highpass_filter(audio_segment, cutoff_freq=150, order=4):
    sample_rate = audio_segment.frame_rate
    channels = audio_segment.channels
    samples = np.array(audio_segment.get_array_of_samples(), dtype=np.float64)
    if channels > 1:
        samples = samples.reshape((-1, channels))

    nyquist = sample_rate / 2.0
    sos = butter(order, min(cutoff_freq / nyquist, 0.99), btype="high", output="sos")

    if channels > 1:
        filtered = np.zeros_like(samples)
        for ch in range(channels):
            filtered[:, ch] = sosfilt(sos, samples[:, ch])
        filtered = filtered.flatten()
    else:
        filtered = sosfilt(sos, samples)

    max_val = 2 ** (audio_segment.sample_width * 8 - 1) - 1
    filtered = np.clip(filtered, -max_val, max_val).astype(
        np.int16 if audio_segment.sample_width == 2 else np.int32,
    )

    return AudioSegment(
        data=filtered.tobytes(),
        sample_width=audio_segment.sample_width,
        frame_rate=sample_rate,
        channels=channels,
    )


def _shape_single_channel(samples, sample_rate, band_gains, band_edges):
    nperseg = min(4096, len(samples))
    freqs, times, Zxx = stft(samples, fs=sample_rate, nperseg=nperseg)

    gain_curve = np.ones(len(freqs))
    for i in range(len(band_gains)):
        mask = (freqs >= band_edges[i]) & (freqs < band_edges[i + 1])
        gain_curve[mask] = 10 ** (band_gains[i] / 20.0)

    Zxx_shaped = Zxx * gain_curve[:, np.newaxis]
    _, result = istft(Zxx_shaped, fs=sample_rate, nperseg=nperseg)

    if len(result) > len(samples):
        result = result[: len(samples)]
    elif len(result) < len(samples):
        result = np.pad(result, (0, len(samples) - len(result)))
    return result


def apply_spectral_shaping(audio_segment, band_gains, band_edges):
    sample_rate = audio_segment.frame_rate
    channels = audio_segment.channels
    samples = np.array(audio_segment.get_array_of_samples(), dtype=np.float64)

    if channels > 1:
        samples = samples.reshape((-1, channels))
        result_channels = [
            _shape_single_channel(samples[:, ch], sample_rate, band_gains, band_edges)
            for ch in range(channels)
        ]
        result = np.column_stack(result_channels).flatten()
    else:
        result = _shape_single_channel(samples, sample_rate, band_gains, band_edges)

    max_val = 2 ** (audio_segment.sample_width * 8 - 1) - 1
    result = np.clip(result, -max_val, max_val).astype(
        np.int16 if audio_segment.sample_width == 2 else np.int32,
    )

    return AudioSegment(
        data=result.tobytes(),
        sample_width=audio_segment.sample_width,
        frame_rate=sample_rate,
        channels=channels,
    )


def apply_dynamic_volume(audio_segment, volume_envelope, window_ms, vol_offset_db=-25, min_vol_db=-40):
    duration_ms = len(audio_segment)
    envelope_median = np.median(volume_envelope)

    chunks = []
    for i, env_level in enumerate(volume_envelope):
        start_ms = i * window_ms
        end_ms = min(start_ms + window_ms, duration_ms)
        if start_ms >= duration_ms:
            break

        chunk = audio_segment[start_ms:end_ms]
        if len(chunk) < 10:
            continue

        deviation = env_level - envelope_median
        dynamic_adjust = np.clip(deviation * 0.4, -6, 6)
        target_volume = max(env_level + vol_offset_db + dynamic_adjust, min_vol_db)

        try:
            gain = np.clip(target_volume - chunk.dBFS, -40, 10)
            chunk = chunk.apply_gain(gain)
        except Exception:
            pass
        chunks.append(chunk)

    if not chunks:
        return audio_segment

    # 一次性合并基于底层内存序列，无损杜绝 O(N²) OOM 溢出及其引发的极长计算耗时
    raw_data = b"".join([c.raw_data for c in chunks])
    result = audio_segment._spawn(raw_data)

    if len(result) > duration_ms:
        result = result[:duration_ms]
    elif len(result) < duration_ms:
        result += AudioSegment.silent(
            duration=duration_ms - len(result),
            frame_rate=audio_segment.frame_rate,
        )
    return result


def apply_stereo_offset(audio_segment, offset=0.3):
    if audio_segment.channels < 2:
        audio_segment = audio_segment.set_channels(2)

    samples = np.array(audio_segment.get_array_of_samples(), dtype=np.float64).reshape((-1, 2))
    left_gain = (1.0 - offset * 0.5) if offset > 0 else 1.0
    right_gain = 1.0 if offset > 0 else (1.0 + offset * 0.5)

    samples[:, 0] *= left_gain
    samples[:, 1] *= right_gain

    max_val = 2 ** (audio_segment.sample_width * 8 - 1) - 1
    result = np.clip(samples.flatten(), -max_val, max_val).astype(
        np.int16 if audio_segment.sample_width == 2 else np.int32,
    )

    return AudioSegment(
        data=result.tobytes(),
        sample_width=audio_segment.sample_width,
        frame_rate=audio_segment.frame_rate,
        channels=2,
    )


# ============================================================================
# 音乐文件检索（原文件行 3483-3494）
# ============================================================================
def get_all_music_files(music_folder):
    supported_extensions = ("*.mp3", "*.wav", "*.flac", "*.ogg", "*.m4a", "*.aac", "*.wma")
    music_files = []
    for ext in supported_extensions:
        music_files.extend(glob.glob(os.path.join(music_folder, ext)))
        music_files.extend(glob.glob(os.path.join(music_folder, ext.upper())))
    music_files = list(set(music_files))
    if not music_files:
        raise FileNotFoundError(f"未找到可选的音乐文件: {music_folder}")
    return music_files


# ============================================================================
# BGM 准备（原文件行 3495-3561）
# ============================================================================
def prepare_copyright_music(
    music_files,
    target_duration_ms,
    original_audio,
    original_analysis,
    vol_offset_db,
    hp_freq,
    fade_ms,
    min_vol_db,
    dyn_vol,
    spec_shape,
    st_offset,
):
    log.info("🎞 开启随机连串版权音乐模式")

    # 全局分析原声频谱间隙
    global_bg, global_be = None, None
    if spec_shape:
        log.info("全局频谱空袭分析与嵌入检测")
        global_bg, global_be = analyze_spectral_gaps(original_audio)

    # 随机打乱音乐库
    shuffled_files = list(music_files)
    random.shuffle(shuffled_files)

    target_seconds = target_duration_ms // 1000
    log.info("BGM 随机拼接池大小: %d 首 | 目标: %d s", len(shuffled_files), target_seconds)

    # ── 收集分段到列表，最终用 raw_data 一次性 O(N) 拼接 ──
    # 旧实现 looped += segment 每轮复制全部已积累数据 → O(N²)
    # 旧实现 looped.fade_out(afade) 每轮再复制一次全量 → 雪上加霜
    segments: list[AudioSegment] = []
    accumulated_ms = 0
    music_idx = 0
    ref_frame_rate = None
    ref_channels = None
    ref_sample_width = None
    _t0 = time.time()

    while accumulated_ms < target_duration_ms:
        music_path = shuffled_files[music_idx % len(shuffled_files)]
        music_idx += 1

        segment = load_music_segment_cached(music_path)
        segment_duration = len(segment)

        # 首次加载时记录参考格式，后续统一归一化以保证 raw_data 拼接安全
        if ref_frame_rate is None:
            ref_frame_rate = segment.frame_rate
            ref_channels = segment.channels
            ref_sample_width = segment.sample_width
        elif (
            segment.frame_rate != ref_frame_rate
            or segment.channels != ref_channels
            or segment.sample_width != ref_sample_width
        ):
            segment = (
                segment
                .set_frame_rate(ref_frame_rate)
                .set_channels(ref_channels)
                .set_sample_width(ref_sample_width)
            )

        if hp_freq > 0:
            segment = apply_highpass_filter(segment, cutoff_freq=hp_freq)
        if spec_shape and global_bg is not None:
            segment = apply_spectral_shaping(segment, global_bg, global_be)

        remaining = target_duration_ms - accumulated_ms

        if remaining < segment_duration:
            segment = segment[:remaining]
            segment = segment.fade_out(min(fade_ms, remaining // 4))
        else:
            segment = segment.fade_out(min(fade_ms, segment_duration // 4))

        # 交叉淡入淡出：只对列表末段做 fade_out（等效于对已积累音频尾部 fade_out）
        # 避免旧实现中 looped.fade_out(afade) 每轮复制全量 O(N) 数据
        if segments and fade_ms > 0:
            afade = min(fade_ms, len(segment) // 4)
            if afade > 0:
                segment = segment.fade_in(afade)
                last = segments[-1]
                segments[-1] = last.fade_out(afade)

        segments.append(segment)
        accumulated_ms += len(segment)

        elapsed = time.time() - _t0
        log.info(
            "BGM 拼接进度: %d/%d s (%d%%) | 已用 %d 首 | 耗时 %.1fs",
            accumulated_ms // 1000,
            target_seconds,
            accumulated_ms * 100 // target_duration_ms,
            music_idx,
            elapsed,
        )

    # ── O(N) 高效拼接：直接连接底层 raw_data，杜绝 O(N²) 反复拷贝 ──
    raw_data = b"".join(s.raw_data for s in segments)
    looped = segments[0]._spawn(raw_data)
    looped = looped[:target_duration_ms]

    log.info("BGM 拼接完成，开始动态音量与淡入淡出后处理...")

    if dyn_vol:
        log.info("全局动态音量包络跟踪")
        env, w_ms = compute_volume_envelope(original_audio)
        looped = apply_dynamic_volume(looped, env, w_ms, vol_offset_db, min_vol_db)
    else:
        target_volume = max(original_analysis["rms_dbfs"] + vol_offset_db, min_vol_db)
        looped = looped.apply_gain(target_volume - looped.dBFS)

    final_fade = min(fade_ms, target_duration_ms // 10)
    if final_fade > 100:
        looped = looped.fade_in(final_fade).fade_out(final_fade)

    if st_offset != 0.0:
        log.info("立体声偏移: %.1f", st_offset)
        looped = apply_stereo_offset(looped, offset=st_offset)

    log.info("BGM 后处理完成，耗时 %.1fs", time.time() - _t0)
    return looped


# ============================================================================
# 顶层混音入口（原文件行 3564-3599）
# ============================================================================
def mix_with_bgm(
    input_path: str,
    output_path: str,
    music_dir: str,
    *,
    volume_offset_db=-25,
    highpass_freq=150,
    fade_duration_ms=3000,
    min_volume_db=-40,
    dyn_vol=True,
    spec_shape=True,
    stereo_offset=0.0,
) -> bool:
    try:
        music_files = get_all_music_files(music_dir)
        log.info("加载原音频: %s", os.path.basename(input_path))
        orig_audio = AudioSegment.from_file(input_path)

        analysis = analyze_audio(orig_audio)
        bgm_music = prepare_copyright_music(
            music_files,
            len(orig_audio),
            orig_audio,
            analysis,
            volume_offset_db,
            highpass_freq,
            fade_duration_ms,
            min_volume_db,
            dyn_vol,
            spec_shape,
            stereo_offset,
        )

        # Format Alignment
        if orig_audio.frame_rate != bgm_music.frame_rate:
            bgm_music = bgm_music.set_frame_rate(orig_audio.frame_rate)
        if orig_audio.channels != bgm_music.channels:
            bgm_music = bgm_music.set_channels(orig_audio.channels)
        if len(bgm_music) > len(orig_audio):
            bgm_music = bgm_music[: len(orig_audio)]
        elif len(bgm_music) < len(orig_audio):
            bgm_music += AudioSegment.silent(
                duration=len(orig_audio) - len(bgm_music),
                frame_rate=orig_audio.frame_rate,
            )

        log.info("🎛️ 混合音频叠加...")
        mixed = orig_audio.overlay(bgm_music)

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        mixed.export(output_path, format="mp3", bitrate="192k")
        log.info("✅ 混音已保存: %s", os.path.basename(output_path))
        return True
    except Exception as e:
        log.error("音频混入失败: %s", e)
        return False