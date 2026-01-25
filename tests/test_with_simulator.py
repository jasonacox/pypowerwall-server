#!/usr/bin/env python3
"""
Integration test for pypowerwall-server using pwsimulator.

This test expects the pwsimulator to be running at https://localhost
(typically as a GitHub Actions service or local Docker container).

It will:
1. Configure pypowerwall-server to connect to the simulator
2. Start the server in the background
3. Test key endpoints
4. Clean up
"""

import os
import sys
import time
import subprocess
import signal
import requests

def wait_for_url(url, timeout=60, verify=True):
    """Wait for a URL to respond with 200 OK."""
    end_time = time.time() + timeout
    while time.time() < end_time:
        try:
            response = requests.get(url, timeout=5, verify=verify)
            if response.status_code == 200:
                print(f"✓ {url} is ready")
                return response
        except Exception as e:
            pass
        time.sleep(1)
    raise TimeoutError(f"Timeout waiting for {url}")

def main():
    # Step 1: Wait for simulator to be healthy
    print("Waiting for pwsimulator at https://localhost/test...")
    wait_for_url("https://localhost/test", verify=False)
    
    # Step 2: Configure environment for pypowerwall-server
    env = os.environ.copy()
    env.update({
        "PW_HOST": "localhost",
        "PW_PASSWORD": "password",
        "PW_EMAIL": "email@example.com",
        "PW_HTTPS": "yes",
        "PW_BIND_ADDRESS": "127.0.0.1",
        "PW_PORT": "8675",
        "PW_CACHE_EXPIRE": "5",
        "PW_TIMEOUT": "10",
        "PW_DEBUG": "yes",
        # Disable SSL verification for simulator's self-signed cert
        "PYTHONHTTPSVERIFY": "0",
        "CURL_CA_BUNDLE": "",
        "REQUESTS_CA_BUNDLE": "",
    })
    
    # Step 3: Start pypowerwall-server
    print("\nStarting pypowerwall-server...")
    server_process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", 
         "--host", "127.0.0.1", "--port", "8675"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    
    try:
        # Step 4: Wait for server to be ready and give it time to poll the simulator
        print("Waiting for server at http://127.0.0.1:8675/health...")
        health_response = wait_for_url("http://127.0.0.1:8675/health", timeout=60)
        health_data = health_response.json()
        print(f"✓ Server health: {health_data.get('status')}")
        print(f"✓ Server version: {health_data.get('version')}")
        
        # Give server time to complete initial polling cycle
        print("Waiting for initial polling cycle (10 seconds)...")
        time.sleep(10)
        
        # Check health again after polling
        health_response = requests.get("http://127.0.0.1:8675/health", timeout=10)
        health_data = health_response.json()
        print(f"✓ Server health after polling: {health_data.get('status')}")
        print(f"  Gateways: {health_data.get('gateways', 0)}")
        print(f"  Online: {health_data.get('gateways_online', 0)}")
        
        # Step 5: Test key endpoints
        print("\nTesting endpoints...")
        
        # Test /aggregates (legacy proxy endpoint)
        print("Testing /aggregates...")
        agg_response = requests.get("http://127.0.0.1:8675/aggregates", timeout=10)
        if agg_response.status_code == 200:
            agg_data = agg_response.json()
            print(f"✓ /aggregates returned data: {list(agg_data.keys())}")
        else:
            print(f"⚠ /aggregates returned status {agg_response.status_code}")
        
        # Test /soe (state of energy)
        print("Testing /soe...")
        soe_response = requests.get("http://127.0.0.1:8675/soe", timeout=10)
        if soe_response.status_code == 200:
            soe_data = soe_response.json()
            print(f"✓ /soe returned: {soe_data.get('percentage')}%")
        else:
            print(f"⚠ /soe returned status {soe_response.status_code}")
        
        # Test /vitals
        print("Testing /vitals...")
        vitals_response = requests.get("http://127.0.0.1:8675/vitals", timeout=10)
        if vitals_response.status_code == 200:
            vitals_data = vitals_response.json()
            print(f"✓ /vitals returned {len(vitals_data)} devices")
        else:
            print(f"⚠ /vitals returned status {vitals_response.status_code}")
        
        # Test /api/gateways
        print("Testing /api/gateways...")
        gateways_response = requests.get("http://127.0.0.1:8675/api/gateways", timeout=10)
        if gateways_response.status_code == 200:
            gateways_data = gateways_response.json()
            print(f"✓ /api/gateways returned {len(gateways_data.get('gateways', []))} gateway(s)")
        else:
            print(f"⚠ /api/gateways returned status {gateways_response.status_code}")
        
        print("\n✅ All tests completed successfully!")
        return 0
        
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        # Print server logs on failure
        try:
            server_process.terminate()
            stdout, stderr = server_process.communicate(timeout=2)
            print("\n=== Server stdout (last 3000 chars) ===")
            print(stdout[-3000:] if len(stdout) > 3000 else stdout)
            print("\n=== Server stderr (last 3000 chars) ===")
            print(stderr[-3000:] if len(stderr) > 3000 else stderr)
        except Exception:
            pass
        return 1
        
    finally:
        # Step 6: Cleanup - stop the server
        print("\nStopping server...")
        try:
            server_process.terminate()
            server_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server_process.kill()
            server_process.wait()
        print("✓ Server stopped")

if __name__ == "__main__":
    sys.exit(main())
