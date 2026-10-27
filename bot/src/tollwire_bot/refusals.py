"""Что сказать человеку, когда панель отказала выдать ссылку на конфиг: текст зависит от причины (reason в 409)."""

# Причина -> ключ текста. Прочие причины (в том числе неизвестные новые) получают общий ответ conflict_generic.
CONFIG_REFUSAL_TEXT = {
    "config_unavailable": "config_unavailable",
    "no_access": "config_no_access",
    "not_ready": "config_not_ready",
    "not_configured": "config_not_configured",
}
