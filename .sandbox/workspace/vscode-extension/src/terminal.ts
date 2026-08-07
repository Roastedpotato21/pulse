import * as vscode from "vscode";
import { PulseRpcClient } from "./rpcClient";

export function registerTerminalIntegration(
  context: vscode.ExtensionContext,
  rpcClient: PulseRpcClient
): void {
  context.subscriptions.push(
    vscode.window.onDidCloseTerminal((terminal: vscode.Terminal) => {
      if (terminal.exitStatus && terminal.exitStatus.code !== 0) {
        vscode.window
          .showWarningMessage(
            `Terminal '${terminal.name}' exited with error code ${terminal.exitStatus.code}. Debug with Pulse?`,
            "Debug with Pulse"
          )
          .then((selection) => {
            if (selection === "Debug with Pulse") {
              vscode.commands.executeCommand("pulse.debugTerminalError", terminal.name, terminal.exitStatus?.code);
            }
          });
      }
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand(
      "pulse.debugTerminalError",
      async (terminalName: string, exitCode?: number) => {
        try {
          const prompt = `Terminal '${terminalName}' failed with exit code ${exitCode ?? "unknown"}. Inspect workspace logs and analyze failure.`;
          const result = (await rpcClient.request<{ content?: string }>("pulse.ask", {
            prompt,
            context: [`Terminal: ${terminalName}`, `ExitCode: ${exitCode}`],
          })) as { content?: string };

          const advice = result?.content || "Pulse completed terminal diagnosis.";
          vscode.window.showInformationMessage(advice);
        } catch (err) {
          const message = err instanceof Error ? err.message : String(err);
          vscode.window.showErrorMessage(`Pulse terminal debugging failed: ${message}`);
        }
      }
    )
  );
}
