import * as vscode from "vscode";
import WebSocket from "ws";

type RpcResponse = { result?: { content?: string }; error?: { message: string } };

class PulseRpcClient {
  private requestId = 0;

  async request(method: string, params: Record<string, unknown>): Promise<string> {
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
        resolve(response.result?.content ?? "Pulse returned no content.");
      });
    });
  }
}

class PulseChatView implements vscode.WebviewViewProvider {
  static readonly viewType = "pulse.chat";
  private view?: vscode.WebviewView;

  constructor(private readonly client: PulseRpcClient) {}

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    view.webview.options = { enableScripts: true };
    view.webview.html = this.html();
    view.webview.onDidReceiveMessage(async ({ type, prompt }) => {
      if (type !== "prompt" || typeof prompt !== "string") { return; }
      await this.send(prompt);
    });
  }

  async send(prompt: string, context: string[] = []): Promise<void> {
    try {
      const content = await this.client.request("pulse.ask", { prompt, context });
      this.view?.webview.postMessage({ type: "response", prompt, content });
      if (!this.view) { vscode.window.showInformationMessage(content); }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.view?.webview.postMessage({ type: "error", content: message });
      if (!this.view) { vscode.window.showErrorMessage(message); }
    }
  }

  private html(): string {
    return `<!doctype html><html><body>
      <main id="messages" aria-live="polite"></main>
      <form id="form"><textarea id="prompt" aria-label="Prompt Pulse" rows="4" placeholder="Ask Pulse about this workspace"></textarea><button>Send</button></form>
      <style>body{color:var(--vscode-foreground);font-family:var(--vscode-font-family)}textarea{box-sizing:border-box;width:100%;background:var(--vscode-input-background);color:inherit}button{margin-top:8px}.message{white-space:pre-wrap;margin:12px 0}</style>
      <script>const vscode=acquireVsCodeApi(),form=document.getElementById('form'),input=document.getElementById('prompt'),messages=document.getElementById('messages');form.addEventListener('submit',event=>{event.preventDefault();const prompt=input.value.trim();if(prompt){messages.insertAdjacentHTML('beforeend','<div class="message"><b>You</b> '+prompt.replaceAll('<','&lt;')+'</div>');vscode.postMessage({type:'prompt',prompt});input.value=''}});window.addEventListener('message',event=>{const item=event.data;messages.insertAdjacentHTML('beforeend','<div class="message"><b>Pulse</b> '+String(item.content).replaceAll('<','&lt;')+'</div>')});</script>
    </body></html>`;
  }
}

function selectedContext(): string[] {
  const editor = vscode.window.activeTextEditor;
  if (!editor || editor.selection.isEmpty) { return []; }
  return [`File: ${editor.document.uri.fsPath}\nSelection:\n${editor.document.getText(editor.selection)}`];
}

export function activate(context: vscode.ExtensionContext): void {
  const client = new PulseRpcClient();
  const chat = new PulseChatView(client);
  context.subscriptions.push(vscode.window.registerWebviewViewProvider(PulseChatView.viewType, chat));
  context.subscriptions.push(vscode.commands.registerCommand("pulse.openChat", () => vscode.commands.executeCommand("pulse.chat.focus")));
  context.subscriptions.push(vscode.commands.registerCommand("pulse.sendPrompt", async () => {
    const prompt = await vscode.window.showInputBox({ prompt: "Send a prompt to Pulse" });
    if (prompt) { await chat.send(prompt); }
  }));
  context.subscriptions.push(vscode.commands.registerCommand("pulse.explainSelection", () => chat.send("Explain this selection.", selectedContext())));
  context.subscriptions.push(vscode.commands.registerCommand("pulse.reviewSelection", () => chat.send("Review this selection for correctness and improvements.", selectedContext())));
  context.subscriptions.push(vscode.commands.registerCommand("pulse.verifyWorkspace", () => client.request("pulse.command", { name: "verify" }).then(vscode.window.showInformationMessage, error => vscode.window.showErrorMessage(String(error)))));
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
