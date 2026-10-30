"""Public strategy brands. Internal names, TXT keys and operation IDs stay stable."""
import re

STRATEGY_BRANDS = (
    ('Momentum', 'Momentum.txt', 'Everest'),
    ('BreaKout', 'BreaKout.txt', 'K2'),
    ('Mean Reversion', 'Mean_Reversion.txt', 'Annapurna'),
    ('Trend Following', 'TrendFollowing.txt', 'Makalu'),
    ('Pairs Trading', 'PairsTrading.txt', 'Manaslu'),
    ('Sector Rotation', 'SectorRotation.txt', 'Dhaulagiri'),
    ('Follow The Money', 'Follow_The_Money.txt', 'Nanga Parbat'),
    ('Entrada Dinero Direccional', 'Entrada_Dinero_Direccional.txt', 'Kangchenjunga'),
    ('Acumulacion', 'Acumulacion.txt', 'Lhotse'),
    ('Acumula Metales', 'Acumula_Metales.txt', 'Cho Oyu'),
)


def _key(value):
    return re.sub(r'[^a-z0-9]', '', str(value or '').removesuffix('.txt').lower())


_PUBLIC = {_key(value): brand for name, txt, brand in STRATEGY_BRANDS for value in (name, txt, brand)}
_CANONICAL = {_key(brand): name for name, txt, brand in STRATEGY_BRANDS}


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
        return result
    return payload
