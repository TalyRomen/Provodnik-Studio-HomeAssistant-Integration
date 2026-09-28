"""Константы интеграции Provodnik."""

DOMAIN = "provodnik"

# Файл общей памяти: .storage/provodnik.memory (в резервных копиях HA)
STORAGE_KEY = "provodnik.memory"

# Версия схемы хранимого объекта. Меняется, только если меняется структура.
STORAGE_VERSION = 1

# Реестр публикаций Studio: .storage/provodnik.publications
PUBLICATIONS_KEY = "provodnik.publications"
