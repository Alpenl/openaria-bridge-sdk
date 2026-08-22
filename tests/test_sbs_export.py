from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import main


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _write_artifact(
    session_dir: Path,
    display_path: str,
    role: str,
    body: bytes,
    media_type: str | None = None,
) -> dict:
    path = session_dir / display_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    entry = {
        "display_path": display_path,
        "role": role,
        "size_bytes": len(body),
        "sha256": _digest(body),
    }
    if media_type is not None:
        entry["media_type"] = media_type
    return entry


def _artifact_entry_for_existing(
    session_dir: Path, display_path: str, role: str, media_type: str | None = None
) -> dict:
    body = (session_dir / display_path).read_bytes()
    entry = {
        "display_path": display_path,
        "role": role,
        "size_bytes": len(body),
        "sha256": _digest(body),
    }
    if media_type is not None:
        entry["media_type"] = media_type
    return entry


def _write_publication_session(
    session_dir: Path, files: list[dict], *, video_codec: str = "h264"
) -> main.Session:
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "session.json").write_text(
        json.dumps({"camera": {"video_codec": video_codec}}), encoding="utf-8"
    )
    manifest = {
        "session_id": "capture-001",
        "captured_at": "2026-08-11T00:00:00Z",
        "duration_seconds": 2.0,
        "integrity_ok": True,
        "files": files,
    }
    (session_dir / "publication_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return main.read_publication_session(session_dir)


def _concat_listing(*paths: Path) -> str:
    return "".join(
        f"file '{main._ffmpeg_concat_path(path.resolve())}'\n" for path in paths
    )


def _run_ffmpeg(args: list[str]) -> None:
    completed = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def _generate_h264_clip(path: Path, color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:size=32x32:rate=10:duration=0.6",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ]
    )


def _generate_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.6",
            "-c:a",
            "pcm_s16le",
            str(path),
        ]
    )


def test_split_eye_export_plan_hstacks_concat_and_muxes_manifest_audio(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    files = [
        _write_artifact(
            session_dir, "video/left_10.mp4", "video_left", b"left 10", "video/mp4"
        ),
        _write_artifact(
            session_dir, "video/right_10.mp4", "video_right", b"right 10", "video/mp4"
        ),
        _write_artifact(
            session_dir, "video/left_2.mp4", "video_left", b"left 2", "video/mp4"
        ),
        _write_artifact(
            session_dir, "video/right_2.mp4", "video_right", b"right 2", "video/mp4"
        ),
        _write_artifact(
            session_dir, "sound/capture.m4a", "other", b"audio", "audio/mp4"
        ),
    ]
    session = _write_publication_session(session_dir, files)
    main.verify(session)

    output = tmp_path / "out.mp4"
    workdir = tmp_path / "work"
    plan = main.build_sbs_export_plan(
        session, output, workdir, preset="ultrafast", audio_bitrate="160k"
    )
    arguments = main.build_sbs_export_ffmpeg_arguments(plan)

    left_listing = workdir / "sbs-left-segments.txt"
    right_listing = workdir / "sbs-right-segments.txt"
    audio_listing = workdir / "sbs-audio-segments.txt"
    assert plan.mode == "split"
    assert plan.crf == main.CRF_FOR_H264_SOURCE
    assert plan.left_segments == (
        session_dir / "video" / "left_2.mp4",
        session_dir / "video" / "left_10.mp4",
    )
    assert plan.right_segments == (
        session_dir / "video" / "right_2.mp4",
        session_dir / "video" / "right_10.mp4",
    )
    assert plan.audio_segments == (session_dir / "sound" / "capture.m4a",)
    assert left_listing.read_text(encoding="utf-8") == _concat_listing(
        session_dir / "video" / "left_2.mp4",
        session_dir / "video" / "left_10.mp4",
    )
    assert right_listing.read_text(encoding="utf-8") == _concat_listing(
        session_dir / "video" / "right_2.mp4",
        session_dir / "video" / "right_10.mp4",
    )
    assert audio_listing.read_text(encoding="utf-8") == _concat_listing(
        session_dir / "sound" / "capture.m4a"
    )
    assert arguments == [
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(left_listing),
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(right_listing),
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(audio_listing),
        "-filter_complex",
        "[0:v:0]setpts=PTS-STARTPTS[l];"
        "[1:v:0]setpts=PTS-STARTPTS[r];"
        "[l][r]hstack=inputs=2[v]",
        "-map",
        "[v]",
        "-map",
        "2:a:0",
        "-c:v",
        "libx264",
        "-profile:v",
        "high",
        "-preset",
        "ultrafast",
        "-crf",
        str(main.CRF_FOR_H264_SOURCE),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-shortest",
        "-sn",
        "-dn",
        "-movflags",
        "+faststart",
        str(output),
    ]


def test_stereo_export_plan_concats_video_and_outputs_no_audio_by_default(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    files = [
        _write_artifact(
            session_dir,
            "spool/source_00010.mp4",
            "video_stereo",
            b"stereo 10",
            "video/mp4",
        ),
        _write_artifact(
            session_dir,
            "spool/source_00002.mp4",
            "video_stereo",
            b"stereo 2",
            "video/mp4",
        ),
    ]
    session = _write_publication_session(session_dir, files, video_codec="mjpeg")

    output = tmp_path / "out.mp4"
    workdir = tmp_path / "work"
    plan = main.build_sbs_export_plan(session, output, workdir)
    arguments = main.build_sbs_export_ffmpeg_arguments(plan)

    listing = workdir / "sbs-stereo-segments.txt"
    assert plan.mode == "stereo"
    assert plan.crf == main.CRF_FOR_MJPEG_SOURCE
    assert plan.stereo_segments == (
        session_dir / "spool" / "source_00002.mp4",
        session_dir / "spool" / "source_00010.mp4",
    )
    assert listing.read_text(encoding="utf-8") == _concat_listing(
        session_dir / "spool" / "source_00002.mp4",
        session_dir / "spool" / "source_00010.mp4",
    )
    assert "-filter_complex" not in arguments
    assert arguments[:8] == [
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(listing),
        "-map",
        "0:v:0",
    ]
    assert "-an" in arguments
    assert "-c:a" not in arguments


def test_export_plan_discovers_unmanifested_wav_in_audio_directory(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    files = [
        _write_artifact(session_dir, "video/left.mp4", "video_left", b"left"),
        _write_artifact(session_dir, "video/right.mp4", "video_right", b"right"),
    ]
    session = _write_publication_session(session_dir, files)
    audio = session_dir / "audio" / "0001.wav"
    audio.parent.mkdir()
    audio.write_bytes(b"local wav")

    plan = main.build_sbs_export_plan(
        session, tmp_path / "out.mp4", tmp_path / "work"
    )

    assert plan.audio_segments == (audio,)


def test_export_plan_rejects_unpaired_split_eye_artifacts(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    files = [
        _write_artifact(session_dir, "video/left.mp4", "video_left", b"left"),
    ]
    session = _write_publication_session(session_dir, files)

    with pytest.raises(main.PipelineError, match="evenly paired"):
        main.build_sbs_export_plan(session, tmp_path / "out.mp4", tmp_path / "work")


def test_export_plan_rejects_mismatched_split_eye_segment_numbers(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    files = [
        _write_artifact(session_dir, "video/left_00001.mp4", "video_left", b"left 1"),
        _write_artifact(session_dir, "video/left_00003.mp4", "video_left", b"left 3"),
        _write_artifact(
            session_dir, "video/right_00001.mp4", "video_right", b"right 1"
        ),
        _write_artifact(
            session_dir, "video/right_00002.mp4", "video_right", b"right 2"
        ),
    ]
    session = _write_publication_session(session_dir, files)

    with pytest.raises(main.PipelineError, match="segment numbers differ"):
        main.build_sbs_export_plan(session, tmp_path / "out.mp4", tmp_path / "work")


def test_export_plan_rejects_mixed_stereo_and_split_eye_artifacts(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    files = [
        _write_artifact(session_dir, "spool/source_00000.mp4", "video_stereo", b"sbs"),
        _write_artifact(session_dir, "video/left_00000.mp4", "video_left", b"left"),
        _write_artifact(session_dir, "video/right_00000.mp4", "video_right", b"right"),
    ]
    session = _write_publication_session(session_dir, files)

    with pytest.raises(main.PipelineError, match="mixes video_stereo"):
        main.build_sbs_export_plan(session, tmp_path / "out.mp4", tmp_path / "work")


def test_read_publication_session_rejects_malformed_manifest_without_traceback(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "publication_manifest.json").write_text(
        json.dumps({"integrity_ok": True, "files": []}), encoding="utf-8"
    )

    with pytest.raises(main.PipelineError, match="field session_id"):
        main.read_publication_session(session_dir)


def test_concat_listing_escapes_ffmpeg_paths() -> None:
    assert (
        main._ffmpeg_concat_path(Path("/tmp/odd 'name'/clip\\01.mp4"))
        == "/tmp/odd '\\''name'\\''/clip\\01.mp4"
    )


def test_export_sbs_api_verifies_inputs_and_invokes_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_dir = tmp_path / "session"
    files = [
        _write_artifact(session_dir, "video/left.mp4", "video_left", b"left"),
        _write_artifact(session_dir, "video/right.mp4", "video_right", b"right"),
    ]
    session = _write_publication_session(session_dir, files)
    captured: dict[str, list[str] | str] = {}

    def fake_run_ffmpeg(arguments: list[str], description: str) -> None:
        captured["arguments"] = arguments
        captured["description"] = description
        Path(arguments[-1]).write_bytes(b"mp4")

    monkeypatch.setattr(main, "run_ffmpeg", fake_run_ffmpeg)

    output = main.export_sbs(
        session,
        tmp_path / "out.mp4",
        workdir=tmp_path / "work",
        preset="ultrafast",
    )

    assert output == tmp_path / "out.mp4"
    assert captured["description"] == "exporting session as SBS MP4"
    assert "hstack=inputs=2" in " ".join(captured["arguments"])
    assert output.read_bytes() == b"mp4"


def test_export_sbs_refuses_existing_output_without_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_dir = tmp_path / "session"
    files = [
        _write_artifact(session_dir, "video/left.mp4", "video_left", b"left"),
        _write_artifact(session_dir, "video/right.mp4", "video_right", b"right"),
    ]
    session = _write_publication_session(session_dir, files)
    output = tmp_path / "out.mp4"
    output.write_bytes(b"old output")

    def fake_run_ffmpeg(arguments: list[str], description: str) -> None:
        raise AssertionError("ffmpeg should not run when output already exists")

    monkeypatch.setattr(main, "run_ffmpeg", fake_run_ffmpeg)

    with pytest.raises(main.PipelineError, match="already exists"):
        main.export_sbs(session, output, workdir=tmp_path / "work")

    assert output.read_bytes() == b"old output"
    assert not (tmp_path / "work").exists()


def test_export_sbs_failure_keeps_existing_output_and_removes_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_dir = tmp_path / "session"
    files = [
        _write_artifact(session_dir, "video/left.mp4", "video_left", b"left"),
        _write_artifact(session_dir, "video/right.mp4", "video_right", b"right"),
    ]
    session = _write_publication_session(session_dir, files)
    output = tmp_path / "out.mp4"
    output.write_bytes(b"old output")

    def fake_run_ffmpeg(arguments: list[str], description: str) -> None:
        assert description == "exporting session as SBS MP4"
        Path(arguments[-1]).write_bytes(b"partial")
        raise main.PipelineError("forced ffmpeg failure")

    monkeypatch.setattr(main, "run_ffmpeg", fake_run_ffmpeg)

    with pytest.raises(main.PipelineError, match="forced ffmpeg failure"):
        main.export_sbs(
            session,
            output,
            workdir=tmp_path / "work",
            overwrite=True,
        )

    assert output.read_bytes() == b"old output"
    assert not list(tmp_path.glob(".ylx-sbs-export-*"))


def test_export_sbs_cli_passes_force_to_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = object()
    captured: dict[str, object] = {}

    monkeypatch.setattr(main.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(main, "read_publication_session", lambda directory: session)

    def fake_export_sbs(
        session_arg: object,
        output: Path,
        **kwargs: object,
    ) -> Path:
        captured["session"] = session_arg
        captured["output"] = output
        captured.update(kwargs)
        return output

    monkeypatch.setattr(main, "export_sbs", fake_export_sbs)

    output = tmp_path / "out.mp4"
    assert (
        main.export_sbs_cli(
            [
                "--input",
                str(tmp_path / "session"),
                "--output",
                str(output),
                "--force",
            ]
        )
        == 0
    )

    assert captured["session"] is session
    assert captured["output"] == output
    assert captured["overwrite"] is True


def test_export_sbs_cli_reports_malformed_manifest_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "publication_manifest.json").write_text(
        json.dumps({"integrity_ok": True, "files": []}), encoding="utf-8"
    )
    monkeypatch.setattr(main.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert (
        main.export_sbs_cli(
            [
                "--input",
                str(session_dir),
                "--output",
                str(tmp_path / "out.mp4"),
            ]
        )
        == 2
    )

    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "field session_id" in captured.err
    assert "Traceback" not in captured.err


def test_export_sbs_from_directory_real_ffmpeg_outputs_h264_sbs_with_aac(
    tmp_path: Path,
) -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not available")

    session_dir = tmp_path / "source 'quote"
    _generate_h264_clip(session_dir / "video" / "left_00000.mp4", "red")
    _generate_h264_clip(session_dir / "video" / "right_00000.mp4", "blue")
    _generate_wav(session_dir / "audio" / "audio_00000.wav")
    session = _write_publication_session(
        session_dir,
        [
            _artifact_entry_for_existing(
                session_dir, "video/left_00000.mp4", "video_left", "video/mp4"
            ),
            _artifact_entry_for_existing(
                session_dir, "video/right_00000.mp4", "video_right", "video/mp4"
            ),
            _artifact_entry_for_existing(
                session_dir, "audio/audio_00000.wav", "metadata", "audio/wav"
            ),
        ],
    )
    main.verify(session)
    output = tmp_path / "exports" / "sbs.mp4"

    exported = main.export_sbs_from_directory(session_dir, output)

    assert exported == output
    assert output.is_file()
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    streams = json.loads(probe.stdout)["streams"]
    video = next(stream for stream in streams if stream["codec_type"] == "video")
    audio = next(stream for stream in streams if stream["codec_type"] == "audio")
    assert video["codec_name"] == "h264"
    assert video["width"] == 64
    assert video["height"] == 32
    assert audio["codec_name"] == "aac"
