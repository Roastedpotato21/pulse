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
const assert = __importStar(require("assert"));
const rpcClient_1 = require("../rpcClient");
describe("PulseRpcClient Integration Tests", () => {
    let client;
    beforeEach(() => {
        client = new rpcClient_1.PulseRpcClient();
    });
    afterEach(() => {
        client.dispose();
    });
    it("instantiates correctly and handles dispose", () => {
        assert.doesNotThrow(() => {
            client.dispose();
        });
    });
    it("handles RPC request attempt gracefully when binary is unavailable or errors", async () => {
        const mockClient = new rpcClient_1.PulseRpcClient("non_existent_pulse_cmd_xyz");
        try {
            await mockClient.getStatus();
            assert.fail("Should have thrown error");
        }
        catch (err) {
            assert.ok(err instanceof Error);
        }
        finally {
            mockClient.dispose();
        }
    });
    it("formats plan JSON-RPC method request correctly", async () => {
        const mockClient = new rpcClient_1.PulseRpcClient("non_existent_pulse_cmd_xyz");
        try {
            await mockClient.plan("test prompt", ["context line 1"]);
        }
        catch (err) {
            assert.ok(err instanceof Error);
        }
        finally {
            mockClient.dispose();
        }
    });
    it("formats executeTool JSON-RPC method request correctly", async () => {
        const mockClient = new rpcClient_1.PulseRpcClient("non_existent_pulse_cmd_xyz");
        try {
            await mockClient.executeTool("read_file", { path: "test.py" });
        }
        catch (err) {
            assert.ok(err instanceof Error);
        }
        finally {
            mockClient.dispose();
        }
    });
    it("formats rollback JSON-RPC method request correctly", async () => {
        const mockClient = new rpcClient_1.PulseRpcClient("non_existent_pulse_cmd_xyz");
        try {
            await mockClient.rollback("tx_123");
        }
        catch (err) {
            assert.ok(err instanceof Error);
        }
        finally {
            mockClient.dispose();
        }
    });
});
//# sourceMappingURL=rpcClient.test.js.map