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
exports.registerTerminalIntegration = registerTerminalIntegration;
const vscode = __importStar(require("vscode"));
function registerTerminalIntegration(context, rpcClient) {
    context.subscriptions.push(vscode.window.onDidCloseTerminal((terminal) => {
        if (terminal.exitStatus && terminal.exitStatus.code !== 0) {
            vscode.window
                .showWarningMessage(`Terminal '${terminal.name}' exited with error code ${terminal.exitStatus.code}. Debug with Pulse?`, "Debug with Pulse")
                .then((selection) => {
                if (selection === "Debug with Pulse") {
                    vscode.commands.executeCommand("pulse.debugTerminalError", terminal.name, terminal.exitStatus?.code);
                }
            });
        }
    }));
    context.subscriptions.push(vscode.commands.registerCommand("pulse.debugTerminalError", async (terminalName, exitCode) => {
        try {
            const prompt = `Terminal '${terminalName}' failed with exit code ${exitCode ?? "unknown"}. Inspect workspace logs and analyze failure.`;
            const result = (await rpcClient.request("pulse.ask", {
                prompt,
                context: [`Terminal: ${terminalName}`, `ExitCode: ${exitCode}`],
            }));
            const advice = result?.content || "Pulse completed terminal diagnosis.";
            vscode.window.showInformationMessage(advice);
        }
        catch (err) {
            const message = err instanceof Error ? err.message : String(err);
            vscode.window.showErrorMessage(`Pulse terminal debugging failed: ${message}`);
        }
    }));
}
//# sourceMappingURL=terminal.js.map