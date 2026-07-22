import * as vscode from "vscode";
import { PulseRpcClient } from "./rpcClient";

export class PulseInlineEditProvider implements vscode.InlineCompletionItemProvider {
  constructor(private readonly rpcClient: PulseRpcClient) {}

  public async provideInlineCompletions(
    document: vscode.TextDocument,
    position: vscode.Position,
    context: vscode.InlineCompletionContext,
    token: vscode.CancellationToken
  ): Promise<vscode.InlineCompletionItem[] | vscode.InlineCompletionList | null> {
    if (token.isCancellationRequested) {
      return null;
    }

    const prefix = document.getText(
      new vscode.Range(new vscode.Position(Math.max(0, position.line - 10), 0), position)
    );
    const suffix = document.getText(
      new vscode.Range(position, new vscode.Position(Math.min(document.lineCount - 1, position.line + 10), 0))
    );

    const docContext = [
      `File: ${document.uri.fsPath}`,
      `Language: ${document.languageId}`,
      `Prefix:\n${prefix}`,
      `Suffix:\n${suffix}`,
    ];

    try {
      const result = (await this.rpcClient.request<{ content?: string; diff?: string }>(
        "pulse.codeAction",
        {
          prompt: "Generate inline edit completion for current cursor position.",
          context: docContext,
        }
      )) as { content?: string; diff?: string };

      const suggestion = result?.content || result?.diff || "";
      if (!suggestion) {
        return null;
      }

      const item = new vscode.InlineCompletionItem(suggestion, new vscode.Range(position, position));
      return [item];
    } catch {
      return null;
    }
  }
}

export function registerInlineEditProvider(
  context: vscode.ExtensionContext,
  rpcClient: PulseRpcClient
): vscode.Disposable {
  const provider = new PulseInlineEditProvider(rpcClient);
  return vscode.languages.registerInlineCompletionItemProvider({ scheme: "file" }, provider);
}
