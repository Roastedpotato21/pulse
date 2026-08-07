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
Object.defineProperty(exports, "__esModule", { value: true });
exports.PulseDiagnosticsFixProvider = void 0;
exports.registerDiagnosticsListener = registerDiagnosticsListener;
const vscode = __importStar(require("vscode"));
class PulseDiagnosticsFixProvider {
    rpcClient;
    static providedCodeActionKinds = [vscode.CodeActionKind.QuickFix];
    constructor(rpcClient) {
        this.rpcClient = rpcClient;
    }
    provideCodeActions(document, range, context) {
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
exports.PulseDiagnosticsFixProvider = PulseDiagnosticsFixProvider;
function registerDiagnosticsListener(context, rpcClient) {
    const codeActionProvider = new PulseDiagnosticsFixProvider(rpcClient);
    context.subscriptions.push(vscode.languages.registerCodeActionsProvider({ scheme: "file" }, codeActionProvider, {
        providedCodeActionKinds: PulseDiagnosticsFixProvider.providedCodeActionKinds,
    }));
    context.subscriptions.push(vscode.commands.registerCommand("pulse.fixDiagnostic", async (document, range, diagnostics) => {
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
            const result = (await rpcClient.explainDiagnostics(diagList, docContext));
            const message = result?.content || "Pulse analyzed diagnostic.";
            vscode.window.showInformationMessage(message);
        }
        catch (err) {
            const message = err instanceof Error ? err.message : String(err);
            vscode.window.showErrorMessage(`Pulse diagnostic fix failed: ${message}`);
        }
    }));
    context.subscriptions.push(vscode.languages.onDidChangeDiagnostics((event) => {
        // Diagnostic change listener hook for reactive telemetry or status updating
    }));
}
//# sourceMappingURL=diagnostics.js.map