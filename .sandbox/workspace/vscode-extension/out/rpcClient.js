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
exports.PulseRpcClient = void 0;
const child_process_1 = require("child_process");
const readline = __importStar(require("readline"));
class PulseRpcClient {
    pythonOrCliPath;
    process = null;
    requestId = 0;
    pendingRequests = new Map();
    constructor(pythonOrCliPath = "pulse") {
        this.pythonOrCliPath = pythonOrCliPath;
        this.startProcess();
    }
    startProcess() {
        try {
            this.process = (0, child_process_1.spawn)(this.pythonOrCliPath, ["rpc"], {
                stdio: ["pipe", "pipe", "ignore"],
                shell: true,
            });
            if (this.process.stdout) {
                const rl = readline.createInterface({ input: this.process.stdout });
                rl.on("line", (line) => this.handleLine(line));
            }
            this.process.on("exit", () => {
                this.process = null;
            });
        }
        catch {
            this.process = null;
        }
    }
    handleLine(line) {
        if (!line.trim())
            return;
        try {
            const response = JSON.parse(line);
            if (typeof response.id === "number" && this.pendingRequests.has(response.id)) {
                const { resolve, reject } = this.pendingRequests.get(response.id);
                this.pendingRequests.delete(response.id);
                if (response.error) {
                    reject(new Error(response.error.message));
                }
                else {
                    resolve(response.result);
                }
            }
        }
        catch {
            // Ignore non-json output lines
        }
    }
    async request(method, params = {}) {
        if (!this.process || !this.process.stdin) {
            this.startProcess();
            if (!this.process || !this.process.stdin) {
                throw new Error("Pulse RPC process is not running.");
            }
        }
        const id = ++this.requestId;
        const payload = JSON.stringify({
            jsonrpc: "2.0",
            id,
            method,
            params,
        }) + "\n";
        return new Promise((resolve, reject) => {
            this.pendingRequests.set(id, { resolve, reject });
            this.process.stdin.write(payload, (err) => {
                if (err) {
                    this.pendingRequests.delete(id);
                    reject(err);
                }
            });
        });
    }
    async plan(prompt, context = []) {
        return this.request("pulse.plan", { prompt, context });
    }
    async executeTool(name, args = {}) {
        return this.request("pulse.executeTool", { name, arguments: args });
    }
    async rollback(transactionId) {
        return this.request("pulse.rollback", { transaction_id: transactionId });
    }
    async getStatus() {
        return this.request("pulse.health", {});
    }
    async explainDiagnostics(diagnostics, context = []) {
        return this.request("pulse.explainDiagnostics", { diagnostics, context });
    }
    async runCommand(command, args = []) {
        return this.request("pulse.runCommand", { command, arguments: args });
    }
    async applyPatch(file, patch) {
        return this.request("pulse.applyPatch", { file, patch });
    }
    dispose() {
        if (this.process) {
            this.process.kill();
            this.process = null;
        }
    }
}
exports.PulseRpcClient = PulseRpcClient;
//# sourceMappingURL=rpcClient.js.map