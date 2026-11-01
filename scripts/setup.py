#!/usr/bin/env python3
"""First-time setup: who Laika talks with, and the secrets. Run once before the first start
(re-running is safe: each question shows the current value as the default).

  python3 scripts/setup.py

It asks for your name, your gender and pronouns (Laika's prompts refer to you with them, and Spanish
and Russian need the gender for adjectives), a short line about you, your time zone, your Tailscale
login and, optionally, a Telegram bot. It writes the `user:` block and `memory.tz` in config.yaml,
and creates .env (mode 600) from .env.example. Nothing is sent anywhere.
Change it later the same way, or edit config.yaml by hand; changing the name after the first start
leaves the old profile folder in the vault.
"""
import os
import pathlib
import re
import stat
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"
ENV, ENV_EXAMPLE = ROOT / ".env", ROOT / ".env.example"
GENDERS = {"1": "woman", "2": "man", "3": "nonbinary", "4": ""}
PRONOUNS = {"woman": "she", "man": "he", "nonbinary": "they", "": "they"}


def ask(question, default="", allowed=None):
    while True:
        shown = f" [{default}]" if default else ""
        try:
            answer = input(f"{question}{shown}: ").strip()
        except EOFError:
            answer = ""
        answer = answer or default
        if allowed is None or answer in allowed:
            return answer
        print(f"  → choose one of: {', '.join(a for a in allowed if a)}")


def current(text, key):
    """A value from the `user:` block (or `  tz:` under memory) of config.yaml."""
    m = re.search(rf"^  {key}:\s*(.*?)\s*(?:#.*)?$", text, re.M)
    return m.group(1).strip().strip('"') if m else ""


def quote(value):
    return f'"{value}"' if re.search(r"[:#'\"{}\[\],&*?|<>=!%@`]", value) or not value else value


def write_user(text, name, pronouns, gender, about):
    block = ("user:                      # who Laika talks with (scripts/setup.py asks for this)\n"
             f"  name: {quote(name)}\n"
             f"  pronouns: {pronouns}            # he | she | they: how her prompts refer to you\n"
             + (f"  gender: {gender}            # woman | man | nonbinary: gendered grammar (Spanish, Russian)\n"
                if gender else "")
             + f"  about: {quote(about)}\n")
    if re.search(r"^user:\s*(?:#.*)?$", text, re.M):
        return re.sub(r"^user:.*?\n(?=\S|\Z)", lambda _: block + "\n", text, count=1, flags=re.M | re.S)
    return block + "\n" + text


def write_env(values):
    if ENV.exists():
        lines = ENV.read_text(encoding="utf-8").splitlines()
    else:
        lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    seen = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in values and not line.lstrip().startswith("#"):
            lines[i] = f"{key}={values[key]}"
            seen.add(key)
    lines += [f"{k}={v}" for k, v in values.items() if k not in seen]
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(ENV, stat.S_IRUSR | stat.S_IWUSR)


def env_value(key):
    if not ENV.exists():
        return ""
    m = re.search(rf"^{key}=(.*)$", ENV.read_text(encoding="utf-8"), re.M)
    return m.group(1).strip() if m else ""


def main():
    if not CONFIG.exists():
        sys.exit("config.yaml not found: run this from the Laika folder")
    text = CONFIG.read_text(encoding="utf-8")
    print("Laika setup. Press Enter to keep the value in brackets.\n")

    name = ask("Your name (Laika calls you this)", current(text, "name") or "Dasha")
    old_gender = current(text, "gender")
    default_g = next((k for k, v in GENDERS.items() if v == old_gender), "4")
    print("Your gender (for Spanish and Russian grammar: cansado/cansada, устал/устала):\n"
          "  1) woman   2) man   3) non-binary   4) prefer not to say")
    gender = GENDERS[ask("Choose 1-4", default_g, set(GENDERS))]
    pronouns = ask("Your pronouns in English (he / she / they)",
                   current(text, "pronouns") or PRONOUNS[gender], {"he", "she", "they"})
    about = ask("One line about you, for her memory (in English)",
                current(text, "about") or "a Spanish speaker who practices English, Russian and Chinese with her")
    tz = ask("Your time zone (IANA name, e.g. Europe/Madrid, Europe/Madrid)",
             current(text, "tz") or "Europe/Madrid")

    text = write_user(text, name, pronouns, gender, about)
    text = re.sub(r"^(  tz:\s*)\S+", lambda m: m.group(1) + tz, text, count=1, flags=re.M)
    CONFIG.write_text(text, encoding="utf-8")
    print(f"\n✓ config.yaml: {name} ({pronouns}{', ' + gender if gender else ''}), time zone {tz}")

    print("\nSecrets (.env, never committed):")
    ts = ask("Your Tailscale login (e.g. <you>@github); only this user can open the app", env_value("ALLOWED_TS_LOGIN"))
    token = ask("Telegram bot token from @BotFather (Enter to skip)", env_value("TELEGRAM_BOT_TOKEN"))
    user_id = ask("Your numeric Telegram user id from @userinfobot (Enter to skip)",
                  env_value("TELEGRAM_ALLOWED_USER_ID")) if token else ""
    write_env({"ALLOWED_TS_LOGIN": ts, "TELEGRAM_BOT_TOKEN": token, "TELEGRAM_ALLOWED_USER_ID": user_id})
    print("✓ .env written (mode 600)")
    if not ts:
        print("  ! ALLOWED_TS_LOGIN is empty: anyone on your tailnet can use the app. Fine for a test only.")
    print("\nNext: docker compose --profile memory up -d --build")


if __name__ == "__main__":
    main()
