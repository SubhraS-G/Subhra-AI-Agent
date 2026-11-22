"""NVIDIA vs Groq latency benchmark (rerunnable: python logs/bench_llm.py).

Pings each provider 3x, then runs 2 real streaming requests per provider,
reporting time-to-first-token (TTFT) and total time. Keys come from
config/.env (GROQ_API_KEY) and agent_config.yaml (NVIDIA).
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from system.llm.client import (
    DEFAULT_BASE_URLS,
    DEFAULT_MODELS,
    LLMClient,
    LLMConfig,
    get_clients,
    load_config,
)

MSGS = [{"role": "user", "content": "Reply with exactly: PONG"}]


def bench(name: str, key: str) -> None:
    cfg = LLMConfig(
        provider=name,
        api_key=key,
        model=DEFAULT_MODELS[name],
        base_url=DEFAULT_BASE_URLS[name],
    )
    cl = LLMClient(cfg)
    print(f"--- {name} / {cfg.model} ---")
    for i in range(3):
        try:
            r = cl.ping()
            print(f"  ping {i + 1}: ok={r['ok']}  {r['latency_ms']} ms  ({r['detail']})")
        except Exception as exc:  # noqa: BLE001
            print(f"  ping {i + 1}: ERROR {type(exc).__name__}: {exc}")
    for i in range(2):
        t0 = time.perf_counter()
        ttft = None
        chars = 0
        try:
            for ev in cl.chat_stream(MSGS):
                if ev.get("type") == "text":
                    if ttft is None:
                        ttft = time.perf_counter() - t0
                    chars += len(ev.get("text", ""))
            total = time.perf_counter() - t0
            ttft_s = f"{ttft * 1000:.0f} ms" if ttft is not None else "n/a"
            print(f"  stream {i + 1}: TTFT={ttft_s}  total={total * 1000:.0f} ms  chars={chars}")
        except Exception as exc:  # noqa: BLE001
            print(f"  stream {i + 1}: ERROR {type(exc).__name__}: {exc}")


def main() -> None:
    nvidia_key = load_config().api_key
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    print("chain configured:", [(c.config.provider, c.config.model) for c in get_clients()])
    if nvidia_key:
        bench("nvidia", nvidia_key)
    else:
        print("nvidia: no key, skipped")
    if groq_key:
        bench("groq", groq_key)
    else:
        print("groq: no key (set GROQ_API_KEY in config/.env), skipped")


if __name__ == "__main__":
    main()
