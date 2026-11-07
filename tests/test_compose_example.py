from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "compose.example.yaml"


def test_compose_example_uses_portable_docker_managed_volumes() -> None:
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    service = document["services"]["tgharbor"]

    assert "user" not in service
    assert service["image"] == "tgharbor:0.2.0"
    assert service["env_file"] == [{"path": ".env", "required": False}]

    mounts = {mount["target"]: mount for mount in service["volumes"]}
    assert mounts["/config/config.yaml"] == {
        "type": "bind",
        "source": "./config.yaml",
        "target": "/config/config.yaml",
        "read_only": True,
    }
    assert mounts["/data"] == {
        "type": "volume",
        "source": "tgharbor_data",
        "target": "/data",
    }
    assert mounts["/downloads"] == {
        "type": "volume",
        "source": "tgharbor_downloads",
        "target": "/downloads",
    }

    volumes = document["volumes"]
    assert set(volumes) == {"tgharbor_data", "tgharbor_downloads"}
    assert all(configuration is None for configuration in volumes.values())


def test_compose_example_contains_no_private_deployment_contract() -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    service = document["services"]["tgharbor"]

    assert "/data/tgharbor/data" not in text
    assert "/data/tgharbor/downloads" not in text
    assert "driver_opts" not in text
    assert "nocopy" not in text
    assert all(
        "name" not in (configuration or {}) for configuration in document["volumes"].values()
    )
    assert not {
        "TGHARBOR_TELEGRAM_API_ID",
        "TGHARBOR_TELEGRAM_API_HASH",
        "TGHARBOR_TELEGRAM_BOT_TOKEN",
        "TGHARBOR_TELEGRAM_PHONE",
        "TGHARBOR_TELEGRAM_AUTHORIZED_USERS",
    }.intersection(service["environment"])
