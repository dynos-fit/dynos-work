"""SubagentStop resolves its helper relative to the plugin, not session cwd."""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_subagent_stop_records_to_non_plugin_project(tmp_path: Path):
    task = tmp_path / ".dynos" / "task-20261018-001"
    task.mkdir(parents=True)
    (task / "manifest.json").write_text(json.dumps({
        "task_id": task.name, "stage": "PLANNING", "created_at": "2026-10-18T12:00:00Z"
    }))
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(json.dumps({"type":"assistant", "message":{
        "model":"fable", "usage":{"input_tokens":12,"output_tokens":4}
    }})+"\n")
    payload = {"cwd":str(tmp_path),"agent_transcript_path":str(transcript),
               "agent_type":"dynos-work:planning","session_id":""}
    proc = subprocess.run([str(ROOT/"hooks/subagent-tokens")], input=json.dumps(payload),
                          text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr
    usage = json.loads((task/"token-usage.json").read_text())
    assert len(usage["events"]) == 1
    assert usage["events"][0]["model"] == "fable"
    assert usage["events"][0]["model_resolution"] == "exact"
