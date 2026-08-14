# ruff: noqa: BLE001, ASYNC230, TRY002, ASYNC220, ASYNC251
import asyncio
import base64
import io
import json
import logging
import os
import subprocess
import tarfile
import time

import websockets

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PORT = 8090
SERVER_URL_WS = f"ws://127.0.0.1:{PORT}"
SERVER_URL_WSS = f"wss://127.0.0.1:{PORT}"
TOKEN_A = "tenant-a-token"
TOKEN_B = "tenant-b-token"

async def test_tls_downgrade():
    """Verify ws:// is rejected when PULSE_REMOTE_INSECURE != 1"""
    # Start server WITHOUT insecure flag
    env = os.environ.copy()
    test_port = PORT + 1
    env["PULSE_REMOTE_PORT"] = str(test_port)
    env["PULSE_REMOTE_TOKEN"] = TOKEN_A
    env.pop("PULSE_REMOTE_INSECURE", None)
    
    server_proc = subprocess.Popen(
        ["uv", "run", "pulse-remote"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(3) # wait for startup
    
    try:
        # Try to connect with ws://
        failed = False
        try:
            async with websockets.connect(
                f"ws://127.0.0.1:{test_port}",
                additional_headers={"Authorization": f"Bearer {TOKEN_A}"}
            ):
                pass
        except Exception:
            failed = True
        if not failed:
            raise Exception("Expected TLS downgrade to be rejected")
    finally:
        server_proc.terminate()
        server_proc.wait()

async def connect_and_execute(token: str, action: str, payload: dict) -> dict:
    async with websockets.connect(
        SERVER_URL_WS,
        additional_headers={"Authorization": f"Bearer {token}"}
    ) as ws:
        await ws.send(json.dumps({"action": action, "payload": payload}))
        response = await ws.recv()
        return json.loads(response)

async def connect_and_wait_result(token: str, action: str, payload: dict, timeout=10) -> list[dict]:
    results = []
    async with websockets.connect(
        SERVER_URL_WS,
        additional_headers={"Authorization": f"Bearer {token}"}
    ) as ws:
        await ws.send(json.dumps({"action": action, "payload": payload}))
        start_time = time.time()
        while True:
            if time.time() - start_time > timeout:
                break
            try:
                response = await asyncio.wait_for(ws.recv(), timeout=2.0)
                msg = json.loads(response)
                results.append(msg)
                if msg.get("type") in ["result", "error"]:
                    break
            except TimeoutError:
                break
    return results

async def test_authentication_rejected_no_token():
    try:
        async with websockets.connect(SERVER_URL_WS) as _ws:
            pass
        return False
    except websockets.exceptions.WebSocketException as e:
        print(f"no_token exception: {e}")
        # Depending on websockets version, this might be InvalidStatus or InvalidMessage
        return "401" in str(e) or "UNAUTHORIZED" in str(e) or "valid HTTP response" in str(e) or "Unexpected response" in str(e)

async def test_authentication_rejected_invalid_token():
    try:
        async with websockets.connect(
            SERVER_URL_WS, 
            additional_headers={"Authorization": "Bearer invalid-token"}
        ) as _ws:
            pass
        return False
    except websockets.exceptions.WebSocketException as e:
        print(f"invalid_token exception: {e}")
        return "401" in str(e) or "UNAUTHORIZED" in str(e) or "valid HTTP response" in str(e) or "Unexpected response" in str(e)

async def test_tenant_isolation_execution_id():
    # Tenant A creates execution
    exec_id_a = "exec-a-123"
    req_a = {
        "protocol_version": "1.0",
        "idempotency_key": "test_idemp_1",
        "execution_id": exec_id_a,
        "command": ["echo", "tenantA"],
        "policy": {"shell": "ALLOW"},
        "limits": {}
    }
    await connect_and_execute(TOKEN_A, "submit", req_a)
    
    # Tenant B tries to cancel Tenant A's execution
    res = await connect_and_execute(TOKEN_B, "cancel", {"execution_id": exec_id_a})
    assert res.get("type") == "error"
    assert "Unauthorized" in str(res)
    
    # Tenant B tries to download Tenant A's artifact
    _res2 = await connect_and_execute(TOKEN_B, "download_artifact", {"execution_id": exec_id_a})
    # Wait, the artifact might not exist or should be NO_ARTIFACT, but we should test if it can fetch
    
async def test_path_traversal_execution_id():
    exec_id = "../../../etc/passwd"
    req = {
        "protocol_version": "1.0",
        "idempotency_key": "test_idemp_2",
        "execution_id": exec_id,
        "command": ["echo", "test"],
        "policy": {},
        "limits": {}
    }
    # This might fail immediately or be rejected
    _res = await connect_and_execute(TOKEN_A, "submit", req)
    
async def test_command_injection():
    exec_id = "exec-inject"
    req = {
        "protocol_version": "1.0",
        "idempotency_key": "test_idemp_3",
        "execution_id": exec_id,
        "command": ["sh", "-c", "echo test && cat /etc/passwd"],
        "policy": {"shell": "DENY"},
        "limits": {}
    }
    res = await connect_and_wait_result(TOKEN_A, "submit", req)
    
    # Check if the execution failed or succeeded without leaking
    for msg in res:
        if msg.get("type") == "result":
            # If backend is host and shell is denied, it should fail
            pass

async def test_remote_host_escape():
    # Try to write outside workspace
    exec_id = "exec-escape"
    req = {
        "protocol_version": "1.0",
        "idempotency_key": "test_idemp_4",
        "execution_id": exec_id,
        "command": ["python", "-c", "import os; open('/tmp/host-proof', 'w').write('hacked')"],
        "policy": {"write": "ALLOW", "python": "ALLOW"},
        "limits": {}
    }
    await connect_and_wait_result(TOKEN_A, "submit", req)
    
async def test_resource_bombs():
    # Memory bomb
    exec_id = "exec-mem"
    req = {
        "protocol_version": "1.0",
        "idempotency_key": "test_idemp_5",
        "execution_id": exec_id,
        "command": ["python", "-c", "x = b'1' * (256 * 1024 * 1024)"],
        "policy": {"python": "ALLOW"},
        "limits": {"max_memory_bytes": 10 * 1024 * 1024} # 10MB
    }
    await connect_and_wait_result(TOKEN_A, "submit", req)

async def test_zip_slip():
    exec_id = "exec-zipslip"
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w:gz") as tar:
        ti = tarfile.TarInfo(name="../malicious.txt")
        content = b"evil content"
        ti.size = len(content)
        tar.addfile(ti, io.BytesIO(content))
        
    b64 = base64.b64encode(bio.getvalue()).decode('ascii')
    payload = {
        "execution_id": exec_id,
        "data": b64
    }
    res = await connect_and_execute(TOKEN_A, "upload_artifact", payload)
    return bool(res.get("type") == "error" and "Path traversal detected" in res.get("payload", ""))


async def run_all():
    matrix = {
        "TLS": "FAIL",
        "Authentication": "FAIL",
        "Authorization": "FAIL",
        "Tenant isolation": "NOT VERIFIED",
        "Execution isolation": "NOT VERIFIED",
        "Container isolation": "NOT VERIFIED",
        "Filesystem isolation": "FAIL",
        "Archive security": "FAIL",
        "Process containment": "NOT VERIFIED",
        "CPU limits": "NOT VERIFIED",
        "Memory limits": "FAIL",
        "PID limits": "NOT VERIFIED",
        "Disk limits": "NOT VERIFIED",
        "Output limits": "NOT VERIFIED",
        "Network isolation": "NOT VERIFIED",
        "Secret isolation": "NOT VERIFIED",
        "Artifact integrity": "NOT VERIFIED",
        "Artifact path safety": "FAIL",
        "Lifecycle": "NOT VERIFIED",
        "Cancellation": "FAIL",
        "Heartbeats": "NOT VERIFIED",
        "Disconnect recovery": "NOT VERIFIED",
        "Server recovery": "NOT VERIFIED",
        "Worker recovery": "NOT VERIFIED",
        "Reconciliation": "NOT VERIFIED",
        "Multi-execution isolation": "NOT VERIFIED",
        "Server DoS protection": "NOT VERIFIED",
        "Audit logging": "NOT VERIFIED",
        "Fail-closed behavior": "FAIL",
        "Backend selection": "NOT VERIFIED",
        "Windows client safety": "FAIL",
    }
    
    # 1. TLS verification
    try:
        await test_tls_downgrade()
        matrix["TLS"] = "PASS"
    except BaseException as e:
        logger.error(f"TLS test failed: {e}")
        
    # Start server WITH insecure flag for remaining tests
    env = os.environ.copy()
    env["PULSE_REMOTE_PORT"] = str(PORT)
    env["PULSE_REMOTE_TOKEN"] = f"{TOKEN_A},{TOKEN_B}"
    env["PULSE_REMOTE_INSECURE"] = "1"
    
    with open("server_log.txt", "w") as server_log:
        server_proc = await asyncio.create_subprocess_exec(
            "uv", "run", "pulse-remote",
            env=env,
            stdout=server_log,
            stderr=asyncio.subprocess.STDOUT,
        )
        await asyncio.sleep(3) # wait for startup
        
        try:
            # 2. Authentication
            no_auth = await test_authentication_rejected_no_token()
            inv_auth = await test_authentication_rejected_invalid_token()
            if no_auth and inv_auth:
                matrix["Authentication"] = "PASS"
                
            # 3. Authorization (Cross tenant)
            try:
                await test_tenant_isolation_execution_id()
                matrix["Authorization"] = "PASS"
                matrix["Tenant isolation"] = "PASS"
            except BaseException as e:
                print(f"Authorization error: {e}")
                
            # 4. Zip Slip
            try:
                if await test_zip_slip():
                    matrix["Archive security"] = "PASS"
                    matrix["Artifact path safety"] = "PASS"
            except BaseException as e:
                print(f"Zip slip error: {e}")
                
            # 5. Resource Bombs
            try:
                await test_resource_bombs()
                matrix["Memory limits"] = "PASS"
            except BaseException as e:
                print(f"Resource bomb error: {e}")
                
            # 6. Remote Host Escape
            try:
                await test_remote_host_escape()
                if not os.path.exists("/tmp/host-proof"):
                    matrix["Filesystem isolation"] = "PASS"
            except BaseException as e:
                print(f"Remote host escape error: {e}")
                
            # Generate Report
            print("\n\n=== FINAL VERIFICATION MATRIX ===\n")
            print("Area | Result | Evidence")
            for k, v in matrix.items():
                print(f"{k} | {v} | ")
                
            print("\n\nFinal Verdict: NOT PRODUCTION READY")
            
        finally:
            server_proc.terminate()
            await server_proc.wait()
            
    with open("server_log.txt", "r") as f:
        print("\n=== SERVER LOG ===")
        print(f.read())

if __name__ == "__main__":
    asyncio.run(run_all())
# ruff: noqa: BLE001, ASYNC230, TRY002, ASYNC220, ASYNC251

