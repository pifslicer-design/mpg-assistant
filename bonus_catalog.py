"""Catalogue centralisé des bonus MPG : mapping API key → UI + stock + consommable."""

# is_consumable=True  : bonus joué activement par le manager (limité par saison)
# is_consumable=False : bonus automatique/structurel — exclu du rapport restants

BONUS_CATALOG: dict[str, dict] = {
    # ── Consommables ────────────────────────────────────────────────────────────
    "boostOnePlayer": {
        "ui_label":      "McDo",
        "short":         "McDo",
        "stock_default": 3,          # 3 utilisations/saison (seul bonus avec stock > 1)
        "is_consumable": True,
    },
    "boostAllPlayers": {
        "ui_label":      "Zahia",
        "short":         "Boost",
        "stock_default": 1,
        "is_consumable": True,
    },
    "removeGoal": {
        "ui_label":      "Valise à Nanard",
        "short":         "Sifflet",
        "stock_default": 1,
        "is_consumable": True,
    },
    "mirror": {
        "ui_label":      "Miroir",
        "short":         "Miroir",
        "stock_default": 1,
        "is_consumable": True,       # copie la compo adverse, joué activement
    },
    "fourStrikers": {
        "ui_label":      "Décathlon",
        "short":         "4 att.",
        "stock_default": 1,
        "is_consumable": True,
    },
    "blockTacticalSubs": {
        "ui_label":      "Tonton Pat'",
        "short":         "Blocage",
        "stock_default": 1,
        "is_consumable": True,
    },
    "nerfGoalkeeper": {
        "ui_label":      "Suarez",
        "short":         "Nérf gk",
        "stock_default": 1,
        "is_consumable": True,
    },
    "nerfAllPlayers": {
        "ui_label":      "Cheat Code",
        "short":         "Nérf",
        "stock_default": 1,
        "is_consumable": True,
    },

    # ── Automatiques (non consommables) ─────────────────────────────────────────
    "captain": {
        "ui_label":      "Capitaine",
        "short":         "Cpt",
        "stock_default": 0,          # obligatoire chaque journée, pas un bonus joué
        "is_consumable": False,
    },
    "boostDefense4": {
        "ui_label":      "Bonus déf. 4",
        "short":         "Déf4",
        "stock_default": 0,          # déclenché automatiquement par la compo (≥4 déf.)
        "is_consumable": False,
    },
    "boostDefense5": {
        "ui_label":      "Bonus déf. 5",
        "short":         "Déf5",
        "stock_default": 0,          # déclenché automatiquement par la compo (≥5 déf.)
        "is_consumable": False,
    },
}

# Liste ordonnée des clés consommables (ordre d'affichage dans le rapport)
CONSUMABLE_KEYS: list[str] = [k for k, v in BONUS_CATALOG.items() if v["is_consumable"]]


def format_bonus_name(api_key: str) -> str:
    """Retourne le label UI MPG, ou la clé brute si inconnue (pas d'exception)."""
    entry = BONUS_CATALOG.get(api_key)
    return entry["ui_label"] if entry else api_key
