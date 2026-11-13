"""K12: Safe operator CLI to inspect or switch TRADING_MODE in .env."""
from __future__ import annotations

import argparse
from pathlib import Path


def switch_mode(env_path: Path, target_mode: str, confirm: bool = False) -> str:
    if target_mode not in ("paper", "live"):
        raise ValueError("target_mode must be 'paper' or 'live'")
    if not env_path.exists():
        raise FileNotFoundError(f"{env_path} not found")
    lines = env_path.read_text(encoding="utf-8").splitlines()
    current = "unknown"
    new_lines = []
    found = False
    for line in lines:
        if line.strip().startswith("TRADING_MODE="):
            current = line.split("=", 1)[1].strip()
            new_lines.append(f"TRADING_MODE={target_mode}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"TRADING_MODE={target_mode}")

    if not confirm:
        return f"DRY-RUN: would switch TRADING_MODE from '{current}' to '{target_mode}' in {env_path} (pass --confirm to apply)."
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return f"APPLIED: switched TRADING_MODE from '{current}' to '{target_mode}' in {env_path}."


def main() -> None:
    parser = argparse.ArgumentParser(description="Switch TRADING_MODE in .env safely.")
    parser.add_argument("--mode", choices=["paper", "live"], required=True)
    parser.add_argument("--env", default=".env")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    print(switch_mode(Path(args.env), args.mode, confirm=args.confirm))


if __name__ == "__main__":
    main()
