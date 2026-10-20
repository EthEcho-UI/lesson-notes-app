"""Speaker diarization with sherpa-onnx (pyannote-segmentation-3.0 + 3D-Speaker CAM++ embedding), no HF token.
Best-effort (benchmark b0b: DER 7-11 % on easy synthetic data, over-splits speakers, real classroom audio unverified)."""
import logging

log = logging.getLogger("lesson_worker.diar")

SEG_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
EMB_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
           "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx")


def models_present(cfg):
    return cfg.diar_segmentation.is_file() and cfg.diar_embedding.is_file()


def diarize(cfg, audio, num_speakers=None):
    """audio: float32 16 kHz mono. Returns [{start,end,speaker:'SPK_n'}] sorted by start."""
    import sherpa_onnx
    sc = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(cfg.diar_segmentation)),
            num_threads=cfg.diarize_threads),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(cfg.diar_embedding), num_threads=cfg.diarize_threads),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=num_speakers or -1, threshold=cfg.diarize_threshold),
        min_duration_on=0.3, min_duration_off=0.5)
    if not sc.validate():
        raise RuntimeError("invalid diarization config / model files")
    sd = sherpa_onnx.OfflineSpeakerDiarization(sc)
    r = sd.process(audio).sort_by_start_time()
    return [{"start": round(s.start, 3), "end": round(s.end, 3), "speaker": f"SPK_{s.speaker}"} for s in r]


def absorb_small_speakers(diar, min_total_s):
    """Over-splitting guard (b0b recommendation): clusters with < min_total_s of speech are re-labelled with the
    nearest-in-time segment of a large cluster. No-op when every cluster is small."""
    tot = {}
    for d in diar:
        tot[d["speaker"]] = tot.get(d["speaker"], 0.0) + d["end"] - d["start"]
    big = {s for s, t in tot.items() if t >= min_total_s}
    if not big or len(big) == len(tot):
        return diar
    bigsegs = [d for d in diar if d["speaker"] in big]
    out = []
    for d in diar:
        if d["speaker"] in big:
            out.append(d)
            continue
        mid = (d["start"] + d["end"]) / 2
        near = min(bigsegs, key=lambda b: 0 if b["start"] <= mid <= b["end"] else min(abs(b["start"] - mid), abs(b["end"] - mid)))
        out.append({**d, "speaker": near["speaker"]})
    return out


def assign_speakers(segs, diar):
    """Max-overlap assignment (b0b merge_speakers.py). Adds raw 'spk' key (None if no overlap)."""
    for w in segs:
        ov = {}
        for d in diar:
            o = min(w["end"], d["end"]) - max(w["start"], d["start"])
            if o > 0:
                ov[d["speaker"]] = ov.get(d["speaker"], 0) + o
        w["spk"] = max(ov, key=ov.get) if ov else None
    return segs


def label_segments(segs):
    """Rename raw labels to contract labels S1, S2... by order of first appearance inside the part; drop 'spk'."""
    names = {}
    out = []
    for s in segs:
        s = dict(s)
        raw = s.pop("spk", None)
        if raw is not None:
            s["speaker"] = names.setdefault(raw, f"S{len(names) + 1}")
        out.append(s)
    return out
