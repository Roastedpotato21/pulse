# Pulse Studio for VS Code

Pulse Studio for VS Code is a modern, feature-rich local client for the Pulse JSON-RPC WebSocket server.

## Run locally

1. From the Pulse workspace, run `pulse serve`.
2. In this folder, run `npm install` then `npm run compile`.
3. Open this folder in VS Code and press `F5` to launch an Extension Development Host.

The extension connects only to `ws://127.0.0.1:8765` by default. Change
`pulse.serverUrl` only when you intentionally run Pulse on a different local
endpoint.

## Features

- **Modern Pulse Chat sidebar**: A beautifully crafted Dark Mode UI with glassmorphism.
- **Simulated Streaming**: Real-time event playback showing reasoning, planning, and task progress.
- **Agent Status**: Visual indicator of what Pulse is currently doing (idle, thinking, working).
- Command Palette prompts and workspace verification.
- Explain and review actions for the editor selection and Quick Fix menu.
- JSON-RPC methods: `pulse.health`, `pulse.askStream`, `pulse.codeAction`, and
  `pulse.command`.

The RPC server deliberately rejects edit and rollback requests because their
existing Pulse approval workflow requires an interactive terminal.
