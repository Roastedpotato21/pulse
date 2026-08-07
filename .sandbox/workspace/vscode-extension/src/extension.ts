import * as vscode from "vscode";
import WebSocket from "ws";

type RpcResponse = { result?: any; error?: { message: string } };

class PulseRpcClient {
  private requestId = 0;

  async request(method: string, params: Record<string, unknown>): Promise<any> {
    const endpoint = vscode.workspace.getConfiguration("pulse").get<string>("serverUrl", "ws://127.0.0.1:8765");
    const id = ++this.requestId;
    return new Promise((resolve, reject) => {
      const socket = new WebSocket(endpoint);
      socket.once("error", () => reject(new Error("Could not connect to Pulse. Start it with `pulse serve`.")));
      socket.once("open", () => socket.send(JSON.stringify({ jsonrpc: "2.0", id, method, params })));
      socket.once("message", (raw) => {
        socket.close();
        const response = JSON.parse(raw.toString()) as RpcResponse;
        if (response.error) { reject(new Error(response.error.message)); return; }
        resolve(response.result);
      });
    });
  }
}

class PulseChatView implements vscode.WebviewViewProvider {
  static readonly viewType = "pulse.chat";
  private view?: vscode.WebviewView;

  constructor(private readonly client: PulseRpcClient, private readonly extensionUri: vscode.Uri) {}

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    view.webview.options = { 
      enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(this.extensionUri, 'media')]
    };
    
    this.html().then(html => {
        view.webview.html = html;
    });

    view.webview.onDidReceiveMessage(async ({ type, prompt }) => {
      if (type !== "prompt" || typeof prompt !== "string") { return; }
      await this.send(prompt);
    });
  }

  async send(prompt: string, context: string[] = []): Promise<void> {
    try {
      const result = await this.client.request("pulse.askStream", { prompt, context });
      if (result && result.events) {
          this.view?.webview.postMessage({ type: "response_events", events: result.events });
      } else if (result && result.content) {
          this.view?.webview.postMessage({ type: "response_events", events: [{ event_type: "llm_token", content: result.content }] });
      } else {
          throw new Error("Pulse returned no events or content.");
      }
      if (!this.view) { vscode.window.showInformationMessage("Pulse responded (open Chat to view)"); }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.view?.webview.postMessage({ type: "error", content: message });
      if (!this.view) { vscode.window.showErrorMessage(message); }
    }
  }

  private async html(): Promise<string> {
    if (!this.view) { return ""; }
    const htmlUri = vscode.Uri.joinPath(this.extensionUri, 'media', 'index.html');
    const styleUri = this.view.webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, 'media', 'style.css'));
    const scriptUri = this.view.webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, 'media', 'main.js'));
    
    try {
        const uint8Array = await vscode.workspace.fs.readFile(htmlUri);
        let htmlStr = new TextDecoder().decode(uint8Array);
        htmlStr = htmlStr.replace('${styleUri}', styleUri.toString());
        htmlStr = htmlStr.replace('${scriptUri}', scriptUri.toString());
        return htmlStr;
    } catch (e) {
        return `<!DOCTYPE html><html><body>Error loading UI: ${e}</body></html>`;
    }
  }
}

function selectedContext(): string[] {
  const editor = vscode.window.activeTextEditor;
  if (!editor || editor.selection.isEmpty) { return []; }
  return [`File: ${editor.document.uri.fsPath}\nSelection:\n${editor.document.getText(editor.selection)}`];
}

export function activate(context: vscode.ExtensionContext): void {
  const client = new PulseRpcClient();
  const chat = new PulseChatView(client, context.extensionUri);
  context.subscriptions.push(vscode.window.registerWebviewViewProvider(PulseChatView.viewType, chat));
  context.subscriptions.push(vscode.commands.registerCommand("pulse.openChat", () => vscode.commands.executeCommand("pulse.chat.focus")));
  context.subscriptions.push(vscode.commands.registerCommand("pulse.sendPrompt", async () => {
    const prompt = await vscode.window.showInputBox({ prompt: "Send a prompt to Pulse" });
    if (prompt) { await chat.send(prompt); }
  }));
  context.subscriptions.push(vscode.commands.registerCommand("pulse.explainSelection", () => chat.send("Explain this selection.", selectedContext())));
  context.subscriptions.push(vscode.commands.registerCommand("pulse.reviewSelection", () => chat.send("Review this selection for correctness and improvements.", selectedContext())));
  context.subscriptions.push(vscode.commands.registerCommand("pulse.verifyWorkspace", () => client.request("pulse.command", { name: "verify" }).then(
      res => vscode.window.showInformationMessage(res?.content ?? "Verified"), 
      error => vscode.window.showErrorMessage(String(error))
  )));
  context.subscriptions.push(vscode.languages.registerCodeActionsProvider({ scheme: "file" }, {
    provideCodeActions(document, range) {
      if (range.isEmpty) { return []; }
      const explain = new vscode.CodeAction("Pulse: Explain selection", vscode.CodeActionKind.QuickFix);
      explain.command = { command: "pulse.explainSelection", title: "Pulse: Explain selection" };
      const review = new vscode.CodeAction("Pulse: Review selection", vscode.CodeActionKind.QuickFix);
      review.command = { command: "pulse.reviewSelection", title: "Pulse: Review selection" };
      return [explain, review];
    }
  }));
}

export function deactivate(): void {}
