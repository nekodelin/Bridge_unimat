import json
from dataclasses import dataclass
from pathlib import Path

from app.models import ChannelConfig, EventTextConfig, ModuleConfig, ModuleGroupConfig


@dataclass(slots=True)
class ConfigBundle:
    signal_map_raw: list[dict]
    event_texts_raw: dict[str, dict]
    module_map_raw: dict
    signal_map: list[ChannelConfig]
    event_texts: dict[str, EventTextConfig]
    modules: dict[str, ModuleConfig]
    groups: dict[str, ModuleGroupConfig]


def load_config_bundle(base_dir: Path | None = None) -> ConfigBundle:
    root_dir = base_dir or Path(__file__).resolve().parent
    signal_map_raw = _load_json(root_dir / "signal_map.json")
    event_texts_raw = _load_json(root_dir / "event_texts.json")
    module_map_raw = _load_json(root_dir / "module_map.json")

    signal_map = [ChannelConfig.model_validate(item) for item in signal_map_raw["channels"]]
    event_texts = {
        signal_id: EventTextConfig.model_validate(config)
        for signal_id, config in event_texts_raw.items()
    }

    modules = {
        module_name: ModuleConfig.model_validate(module_config)
        for module_name, module_config in module_map_raw["modules"].items()
    }
    groups = {
        group_name: ModuleGroupConfig.model_validate(group_config)
        for group_name, group_config in module_map_raw["groups"].items()
    }

    return ConfigBundle(
        signal_map_raw=signal_map_raw["channels"],
        event_texts_raw=event_texts_raw,
        module_map_raw=module_map_raw,
        signal_map=signal_map,
        event_texts=event_texts,
        modules=modules,
        groups=groups,
    )


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
