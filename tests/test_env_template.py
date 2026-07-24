"""The repository ships one canonical environment template (env.example).

These tests fail closed if the template drifts from Settings — either a new
Settings env var is added without documenting it, or a second competing template
file reappears.
"""
import re
from pathlib import Path

from app.settings import Settings

_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATE = _ROOT / "env.example"

# Env keys Settings.from_env actually reads, extracted from the source so this
# check updates itself when a setting is added (values.get("X"...) and the
# _float/_bool/_int/_optional_path(values, "X"...) helpers).
_ENV_KEY_PATTERN = re.compile(
    r'(?:values\.get|_float|_bool|_int|_optional_path)'
    r'\(\s*(?:values,\s*)?"([A-Z][A-Z0-9_]+)"'
)
# Documented keys in the template, including intentionally-commented optionals
# (e.g. "# STATE_DB=...").
_TEMPLATE_KEY_PATTERN = re.compile(r'^\s*#?\s*([A-Z][A-Z0-9_]+)=', re.MULTILINE)


def _settings_env_keys() -> set[str]:
    source = (_ROOT / "app" / "settings.py").read_text(encoding="utf-8")
    return set(_ENV_KEY_PATTERN.findall(source))


def _documented_keys() -> set[str]:
    return set(_TEMPLATE_KEY_PATTERN.findall(_TEMPLATE.read_text(encoding="utf-8")))


def test_only_one_canonical_env_template_exists():
    assert _TEMPLATE.is_file()
    # The dotted duplicate must not reappear and drift from env.example.
    assert not (_ROOT / ".env.example").exists()


def test_env_template_documents_every_settings_variable():
    missing = _settings_env_keys() - _documented_keys()
    assert not missing, f"env.example is missing Settings env vars: {sorted(missing)}"


def test_env_template_loads_into_settings():
    values: dict[str, str] = {}
    for line in _TEMPLATE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    # The template ships APP_ENV=development, so this must construct cleanly.
    settings = Settings.from_env(values)
    assert settings.environment == "development"
    assert settings.provider_clock_skew_seconds == 5.0
