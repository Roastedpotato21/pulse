import * as vscode from "vscode";
import { PulseRpcClient } from "./rpcClient";

export class PulseDiagnosticsFixProvider implements vscode.CodeActionProvider {
  public static readonly providedCodeActionKinds = [vscode.CodeActionKind.QuickFix];

  constructor(private readonly rpcClient: PulseRpcClient) {}

  public provideCodeActions(
    document: vscode.TextDocument,
    range: vscode.Range | vscode.Selection,
    context: vscode.CodeActionContext
  ): vscode.CodeAction[] {
    if (context.diagnostics.length === 0) {
      return [];
    }

    const action = new vscode.CodeAction("Fix with Pulse", vscode.CodeActionKind.QuickFix);
    action.command = {
      command: "pulse.fixDiagnostic",
      title: "Fix with Pulse",
      arguments: [document, range, context.diagnostics],
    };
    action.isPreferred = true;
    return [action];
  }
}

export function registerDiagnosticsListener(
  context: vscode.ExtensionContext,
  rpcClient: PulseRpcClient
): void {
  const codeActionProvider = new PulseDiagnosticsFixProvider(rpcClient);
  context.subscriptions.push(
    vscode.languages.registerCodeActionsProvider({ scheme: "file" }, codeActionProvider, {
      providedCodeActionKinds: PulseDiagnosticsFixProvider.providedCodeActionKinds,
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand(
      "pulse.fixDiagnostic",
      async (document: vscode.TextDocument, range: vscode.Range, diagnostics: vscode.Diagnostic[]) => {
        try {
          const diagList = diagnostics.map((d) => ({
            message: d.message,
            severity: d.severity,
            source: d.source,
            line: d.range.start.line + 1,
          }));

          const docContext = [
            `File: ${document.uri.fsPath}`,
            `Content snippet:\n${document.getText(range)}`,
          ];

          const result = (await rpcClient.explainDiagnostics(diagList, docContext)) as {
            content?: string;
          };

          const message = result?.content || "Pulse analyzed diagnostic.";
          vscode.window.showInformationMessage(message);
        } catch (err) {
          const message = err instanceof Error ? err.message : String(err);
          vscode.window.showErrorMessage(`Pulse diagnostic fix failed: ${message}`);
        }
      }
    )
  );

  context.subscriptions.push(
    vscode.languages.onDidChangeDiagnostics((event: vscode.DiagnosticChangeEvent) => {
      // Diagnostic change listener hook for reactive telemetry or status updating
    })
  );
}
