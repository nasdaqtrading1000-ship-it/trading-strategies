"""Public strategy brands. Internal names, TXT keys and operation IDs stay stable."""
import re

STRATEGY_BRANDS = (
    ('Momentum', 'Momentum.txt', 'Everest (MOM)'),
    ('BreaKout', 'BreaKout.txt', 'K2 (BRK)'),
    ('Mean Reversion', 'Mean_Reversion.txt', 'Annapurna (MRV)'),
    ('Trend Following', 'TrendFollowing.txt', 'Makalu (TRF)'),
    ('Pairs Trading', 'PairsTrading.txt', 'Manaslu (PAR)'),
    ('Sector Rotation', 'SectorRotation.txt', 'Dhaulagiri (SEC)'),
    ('Follow The Money', 'Follow_The_Money.txt', 'Nanga Parbat (FTM)'),
    ('Entrada Dinero Direccional', 'Entrada_Dinero_Direccional.txt', 'Kangchenjunga (EDD)'),
    ('Acumulacion', 'Acumulacion.txt', 'Lhotse (ACU)'),
    ('Acumula Metales', 'Acumula_Metales.txt', 'Cho Oyu (MET)'),
)


def _key(value):
    return re.sub(r'[^a-z0-9]', '', str(value or '').removesuffix('.txt').lower())


_PUBLIC = {_key(value): brand for name, txt, brand in STRATEGY_BRANDS for value in (name, txt, brand, brand.split(' (')[0])}
_CANONICAL = {_key(value): name for name, txt, brand in STRATEGY_BRANDS for value in (brand, brand.split(' (')[0])}
_PYTHON_FILES = dict(zip((name for name, txt, brand in STRATEGY_BRANDS), (
    'Momentum.py', 'BreaKout.py', 'Mean Reversion.py', 'TrendFollowing.py',
    'PairsTrading.py', 'SectorRotation.py', 'FollowTheMoney.py',
    'EntradaDineroDireccional.py', 'Acumulacion.py', 'AcumulaMetales.py',
)))
_TXT_FILES = {name: txt for name, txt, brand in STRATEGY_BRANDS}


def strategy_public_filename(value):
    value = str(value or '')
    stem, dot, extension = value.rpartition('.')
    label = _PUBLIC.get(_key(stem))
    return label.split(' (')[0] + dot + extension if label and extension.lower() in ('txt', 'py') else value


def strategy_internal_filename(value):
    value = str(value or '')
    stem, dot, extension = value.rpartition('.')
    name = strategy_internal_name(stem)
    files = _TXT_FILES if extension.lower() == 'txt' else _PYTHON_FILES if extension.lower() == 'py' else {}
    return files.get(name, value)


_TEXT_ALIASES = {variant: brand for name, txt, brand in STRATEGY_BRANDS
                 for variant in (name, txt[:-4], _PYTHON_FILES[name][:-3])}
_TEXT_PATTERN = re.compile(r'(?<![\w])(' + '|'.join(re.escape(v) for v in sorted(_TEXT_ALIASES, key=len, reverse=True)) + r')(?![\w])')


def strategy_public_text(value):
    """Brand human-facing text only; never apply to engine data or persisted keys."""
    if isinstance(value, dict):
        return {key: strategy_public_text(item) for key, item in value.items()}
    if isinstance(value, list):
        return [strategy_public_text(item) for item in value]
    if isinstance(value, str):
        return _TEXT_PATTERN.sub(lambda match: _TEXT_ALIASES[match.group()], value)
    return value


def strategy_display_name(value, txt_name=None):
    """Match names or stable file keys, without touching IDs or unknown strategies."""
    return _PUBLIC.get(_key(txt_name)) or _PUBLIC.get(_key(value)) or value


def strategy_internal_name(value):
    """Accept public labels from editable admin forms without renaming engine keys."""
    return _CANONICAL.get(_key(value), value)


def strategy_labels(payload):
    """Copy a presentation/API payload; never change technical or financial fields."""
    if isinstance(payload, (list, tuple)):
        return [strategy_labels(item) for item in payload]
    if isinstance(payload, dict):
        result = {key: strategy_labels(value) for key, value in payload.items()}
        txt = payload.get('signals_txt_name') or payload.get('txt_name')
        for key in ('name', 'strategy_name', 'short_name'):
            if key in result:
                result[key] = strategy_display_name(result[key], txt)
        for key in ('message', 'error', 'reason'):
            if key in result:
                result[key] = strategy_public_text(result[key])
        return result
    return payload
