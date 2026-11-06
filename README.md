# TGHarbor

[简体中文](docs/zh-CN/README.md)

**Self-hosted Telegram downloader and archive manager.**

TGHarbor is a clean-room, independently implemented project for downloading,
organizing, and archiving Telegram content.

> Early development: configuration and internal APIs may change before v1.0.

## v0.1 goals

- Telegram authentication
- Authorized-user media intake
- Persistent SQLite download queue
- Asynchronous download workers
- Local storage
- Progress reporting
- Retry and restart recovery
- Docker-first deployment

## Development

See [Development setup](docs/0-foundation/001-development-setup.md) for environment setup,
checks, startup, and configuration.

## Status

The **v0.1 — Core Downloader** implementation is available for local acceptance.
The automated suite covers the queue, recovery, storage, workers, authorization,
Telegram adapters, and lifecycle without live credentials. A real Telegram account,
Bot token, and Docker host are still required for deployment-specific acceptance.

## Documentation

- [Documentation index](docs/README.md)
- [v0.1 implementation and acceptance](docs/2-implementation/201-v0-1-core-downloader.md)
- [Operations guide](docs/3-operations/301-core-downloader-operations.md)
- [中文文档](docs/zh-CN/README.md)

## Acknowledgements

TGHarbor is an independent implementation inspired by the workflows and use
cases of existing Telegram downloader projects, including
`jsavargas/telethon_downloader`.

## License

Apache License 2.0.
