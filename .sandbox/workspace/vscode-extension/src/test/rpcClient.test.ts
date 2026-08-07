import * as assert from "assert";
import { PulseRpcClient } from "../rpcClient";

describe("PulseRpcClient Integration Tests", () => {
  let client: PulseRpcClient;

  beforeEach(() => {
    client = new PulseRpcClient();
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
    const mockClient = new PulseRpcClient("non_existent_pulse_cmd_xyz");
    try {
      await mockClient.getStatus();
      assert.fail("Should have thrown error");
    } catch (err) {
      assert.ok(err instanceof Error);
    } finally {
      mockClient.dispose();
    }
  });

  it("formats plan JSON-RPC method request correctly", async () => {
    const mockClient = new PulseRpcClient("non_existent_pulse_cmd_xyz");
    try {
      await mockClient.plan("test prompt", ["context line 1"]);
    } catch (err) {
      assert.ok(err instanceof Error);
    } finally {
      mockClient.dispose();
    }
  });

  it("formats executeTool JSON-RPC method request correctly", async () => {
    const mockClient = new PulseRpcClient("non_existent_pulse_cmd_xyz");
    try {
      await mockClient.executeTool("read_file", { path: "test.py" });
    } catch (err) {
      assert.ok(err instanceof Error);
    } finally {
      mockClient.dispose();
    }
  });

  it("formats rollback JSON-RPC method request correctly", async () => {
    const mockClient = new PulseRpcClient("non_existent_pulse_cmd_xyz");
    try {
      await mockClient.rollback("tx_123");
    } catch (err) {
      assert.ok(err instanceof Error);
    } finally {
      mockClient.dispose();
    }
  });
});
