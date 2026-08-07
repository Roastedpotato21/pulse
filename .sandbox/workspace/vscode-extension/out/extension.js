"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.activate = activate;
exports.deactivate = deactivate;
const vscode = __importStar(require("vscode"));
const ws_1 = __importDefault(require("ws"));
class PulseRpcClient {
    requestId = 0;
    async request(method, params) {
        const endpoint = vscode.workspace.getConfiguration("pulse").get("serverUrl", "ws://127.0.0.1:8765");
        const id = ++this.requestId;
        return new Promise((resolve, reject) => {
            const socket = new ws_1.default(endpoint);
            socket.once("error", () => reject(new Error("Could not connect to Pulse. Start it with `pulse serve`.")));
            socket.once("open", () => socket.send(JSON.stringify({ jsonrpc: "2.0", id, method, params })));
            socket.once("message", (raw) => {
                socket.close();
                const response = JSON.parse(raw.toString());
                if (response.error) {
                    reject(new Error(response.error.message));
                    return;
                }
                resolve(response.result);
            });
        });
    }
}
class PulseChatView {
    client;
    extensionUri;
    static viewType = "pulse.chat";
    view;
    constructor(client, extensionUri) {
        this.client = client;
        this.extensionUri = extensionUri;
    }
    resolveWebviewView(view) {
        this.view = view;
        view.webview.options = {
            enableScripts: true,
            localResourceRoots: [vscode.Uri.joinPath(this.extensionUri, 'media')]
        };
        this.html().then(html => {
            view.webview.html = html;
        });
        view.webview.onDidReceiveMessage(async ({ type, prompt }) => {
            if (type !== "prompt" || typeof prompt !== "string") {
                return;
            }
            await this.send(prompt);
        });
    }
    async send(prompt, context = []) {
        try {
            const result = await this.client.request("pulse.askStream", { prompt, context });
            if (result && result.events) {
                this.view?.webview.postMessage({ type: "response_events", events: result.events });
            }
            else if (result && result.content) {
                this.view?.webview.postMessage({ type: "response_events", events: [{ event_type: "llm_token", content: result.content }] });
            }
            else {
                throw new Error("Pulse returned no events or content.");
            }
            if (!this.view) {
                vscode.window.showInformationMessage("Pulse responded (open Chat to view)");
            }
        }
        catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            this.view?.webview.postMessage({ type: "error", content: message });
            if (!this.view) {
                vscode.window.showErrorMessage(message);
            }
        }
    }
    async html() {
        if (!this.view) {
            return "";
        }
        const htmlUri = vscode.Uri.joinPath(this.extensionUri, 'media', 'index.html');
        const styleUri = this.view.webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, 'media', 'style.css'));
        const scriptUri = this.view.webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, 'media', 'main.js'));
        try {
            const uint8Array = await vscode.workspace.fs.readFile(htmlUri);
            let htmlStr = new TextDecoder().decode(uint8Array);
            htmlStr = htmlStr.replace('${styleUri}', styleUri.toString());
            htmlStr = htmlStr.replace('${scriptUri}', scriptUri.toString());
            return htmlStr;
        }
        catch (e) {
            return `<!DOCTYPE html><html><body>Error loading UI: ${e}</body></html>`;
        }
    }
}
function selectedContext() {
    const editor = vscode.window.activeTextEditor;
    if (!editor || editor.selection.isEmpty) {
        return [];
    }
    return [`File: ${editor.document.uri.fsPath}\nSelection:\n${editor.document.getText(editor.selection)}`];
}
function activate(context) {
    const client = new PulseRpcClient();
    const chat = new PulseChatView(client, context.extensionUri);
    context.subscriptions.push(vscode.window.registerWebviewViewProvider(PulseChatView.viewType, chat));
    context.subscriptions.push(vscode.commands.registerCommand("pulse.openChat", () => vscode.commands.executeCommand("pulse.chat.focus")));
    context.subscriptions.push(vscode.commands.registerCommand("pulse.sendPrompt", async () => {
        const prompt = await vscode.window.showInputBox({ prompt: "Send a prompt to Pulse" });
        if (prompt) {
            await chat.send(prompt);
        }
    }));
    context.subscriptions.push(vscode.commands.registerCommand("pulse.explainSelection", () => chat.send("Explain this selection.", selectedContext())));
    context.subscriptions.push(vscode.commands.registerCommand("pulse.reviewSelection", () => chat.send("Review this selection for correctness and improvements.", selectedContext())));
    context.subscriptions.push(vscode.commands.registerCommand("pulse.verifyWorkspace", () => client.request("pulse.command", { name: "verify" }).then(res => vscode.window.showInformationMessage(res?.content ?? "Verified"), error => vscode.window.showErrorMessage(String(error)))));
    context.subscriptions.push(vscode.languages.registerCodeActionsProvider({ scheme: "file" }, {
        provideCodeActions(document, range) {
            if (range.isEmpty) {
                return [];
            }
            const explain = new vscode.CodeAction("Pulse: Explain selection", vscode.CodeActionKind.QuickFix);
            explain.command = { command: "pulse.explainSelection", title: "Pulse: Explain selection" };
            const review = new vscode.CodeAction("Pulse: Review selection", vscode.CodeActionKind.QuickFix);
            review.command = { command: "pulse.reviewSelection", title: "Pulse: Review selection" };
            return [explain, review];
        }
    }));
}
function deactivate() { }
//# sourceMappingURL=extension.js.map