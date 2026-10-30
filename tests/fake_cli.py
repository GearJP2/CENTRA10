"""Subprocess fixtures for runner integration tests; never contacts cloud services."""
import json
import os
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen


def main():
    name, args = Path(sys.argv[0]).name, sys.argv[1:]
    folder = Path(os.environ["CENTRA_SIM_DIR"])
    scenario = os.environ.get("CENTRA_SIM_SCENARIO", "success")
    with (folder / "commands.jsonl").open("a") as log:
        log.write(json.dumps([name, *args]) + "\n")
    if name == "kubectl":
        if "scale" in args and "--replicas=0" in args and scenario == "cleanup_fail":
            sys.exit(7)
        if "get" in args or "version" in args:
            print(json.dumps({"items": []}))
        return
    if name == "sky":
        if "--version" in args:
            print("skypilot, version 0.14.1")
        elif "--endpoint" in args:
            print(os.environ["CENTRA_SIM_URL"])
        return
    assert name == "vllm", name
    if args == ["--version"]:
        print("0.32.0")
        return
    assert args[:2] == ["bench", "serve"], args
    if scenario == "benchmark_exit":
        sys.exit(9)
    if scenario == "hang":
        time.sleep(60)
        return

    def flag(key):
        return args[args.index(key) + 1]

    ttfts, errors, start_times = [], [], []
    for _ in range(int(flag("--num-prompts"))):
        started = time.monotonic()
        start_times.append(time.perf_counter())
        request = Request(flag("--base-url") + flag("--endpoint"),
            data=json.dumps({"model": flag("--model"), "stream": True,
                             "messages": [{"role": "user", "content": "synthetic test"}]}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=5) as response:
                first = response.readline()
                assert first.startswith(b"data: "), first
                ttfts.append(time.monotonic() - started)
                response.read()
            errors.append("")
        except Exception as exc:
            ttfts.append(0)
            errors.append(str(exc))
    time.sleep(.08)
    result = {"ttfts": ttfts, "errors": errors, "start_times": start_times, "completed": sum(not e for e in errors)}
    if scenario == "bad_result":
        del result["ttfts"]
    if scenario == "bad_timestamp":
        result["start_times"][0] = 0
    target = Path(flag("--result-dir")) / flag("--result-filename")
    target.write_text(json.dumps(result))


if __name__ == "__main__":
    main()
