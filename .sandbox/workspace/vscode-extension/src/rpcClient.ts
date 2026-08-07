import { ChildProcess, spawn } from "child_process";
import * as readline from "readline";

export interface RpcResponse<T = unknown> {
  jsonrpc: "2.0";
  id: number;
  result?: T;
  error?: {
    code: number;
    message: string;
    data?: unknown;
  };
}

export class PulseRpcClient {
  private process: ChildProcess | null = null;
  private requestId = 0;
  private pendingRequests = new Map<
    number,
    { resolve: (value: any) => void; reject: (reason: any) => void }
  >();

  constructor(private readonly pythonOrCliPath: string = "pulse") {
    this.startProcess();
  }

  private startProcess(): void {
    try {
      this.process = spawn(this.pythonOrCliPath, ["rpc"], {
        stdio: ["pipe", "pipe", "ignore"],
        shell: true,
      });

      if (this.process.stdout) {
        const rl = readline.createInterface({ input: this.process.stdout });
        rl.on("line", (line: string) => this.handleLine(line));
      }

      this.process.on("exit", () => {
        this.process = null;
      });
    } catch {
      this.process = null;
    }
  }

  private handleLine(line: string): void {
    if (!line.trim()) return;
    try {
      const response = JSON.parse(line) as RpcResponse;
      if (typeof response.id === "number" && this.pendingRequests.has(response.id)) {
        const { resolve, reject } = this.pendingRequests.get(response.id)!;
        this.pendingRequests.delete(response.id);
        if (response.error) {
          reject(new Error(response.error.message));
        } else {
          resolve(response.result);
        }
      }
    } catch {
      // Ignore non-json output lines
    }
  }

  public async request<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    if (!this.process || !this.process.stdin) {
      this.startProcess();
      if (!this.process || !this.process.stdin) {
        throw new Error("Pulse RPC process is not running.");
      }
    }

    const id = ++this.requestId;
    const payload =
      JSON.stringify({
        jsonrpc: "2.0",
        id,
        method,
        params,
      }) + "\n";

    return new Promise<T>((resolve, reject) => {
      this.pendingRequests.set(id, { resolve, reject });
      this.process!.stdin!.write(payload, (err: Error | null | undefined) => {
        if (err) {
          this.pendingRequests.delete(id);
          reject(err);
        }
      });
    });
  }

  public async plan(prompt: string, context: string[] = []): Promise<unknown> {
    return this.request("pulse.plan", { prompt, context });
  }

  public async executeTool(name: string, args: Record<string, unknown> = {}): Promise<unknown> {
    return this.request("pulse.executeTool", { name, arguments: args });
  }

  public async rollback(transactionId?: string): Promise<unknown> {
    return this.request("pulse.rollback", { transaction_id: transactionId });
  }

  public async getStatus(): Promise<unknown> {
    return this.request("pulse.health", {});
  }

  public async explainDiagnostics(diagnostics: unknown[], context: string[] = []): Promise<unknown> {
    return this.request("pulse.explainDiagnostics", { diagnostics, context });
  }

  public async runCommand(command: string, args: string[] = []): Promise<unknown> {
    return this.request("pulse.runCommand", { command, arguments: args });
  }

  public async applyPatch(file: string, patch: string): Promise<unknown> {
    return this.request("pulse.applyPatch", { file, patch });
  }

  public dispose(): void {
    if (this.process) {
      this.process.kill();
      this.process = null;
    }
  }
}
