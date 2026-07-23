import sys
import json
import urllib.request
import urllib.parse
import urllib.error
import threading
import time

def sse_stream_listener(url, base_headers, session_id):
    headers = {
        **base_headers,
        "Accept": "text/event-stream",
        "mcp-session-id": session_id
    }
    while True:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req) as response:
                current_event = None
                for line_bytes in response:
                    line = line_bytes.decode("utf-8").strip()
                    if not line:
                        continue
                    if line.startswith("event:"):
                        current_event = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        data = line[len("data:"):].strip()
                        if current_event == "message":
                            sys.stdout.write(data + "\n")
                            sys.stdout.flush()
        except Exception as e:
            # SSE disconnected, log it and try reconnecting in a bit
            print(f"SSE GET Stream disconnected: {e}", file=sys.stderr)
            time.sleep(1)

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 mcp_remote_bridge_pure.py <URL> [--header 'Key: Value']", file=sys.stderr)
        sys.exit(1)
        
    url = sys.argv[1]
    base_headers = {}
    
    i = 2
    while i < len(sys.argv):
        if sys.argv[i] == "--header" and i + 1 < len(sys.argv):
            h = sys.argv[i+1]
            if ":" in h:
                k, v = h.split(":", 1)
                base_headers[k.strip()] = v.strip()
            i += 2
        else:
            i += 1

    session_id = None
    
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
            
        if session_id is None:
            # First request: POST to establish session
            req_headers = {
                **base_headers,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"
            }
            req = urllib.request.Request(
                url,
                data=line.encode("utf-8"),
                headers=req_headers,
                method="POST"
            )
            try:
                with urllib.request.urlopen(req) as response:
                    # Extract session id from headers
                    for k, v in response.headers.items():
                        if k.lower() == "mcp-session-id":
                            session_id = v
                            break
                    
                    content_type = response.headers.get("Content-Type", "").lower()
                    
                    if "text/event-stream" in content_type:
                        current_event = None
                        for line_bytes in response:
                            l = line_bytes.decode("utf-8").strip()
                            if not l:
                                continue
                            if l.startswith("event:"):
                                current_event = l[len("event:"):].strip()
                            elif l.startswith("data:"):
                                data = l[len("data:"):].strip()
                                if current_event == "message":
                                    sys.stdout.write(data + "\n")
                                    sys.stdout.flush()
                                    break
                    else:
                        data = response.read().decode("utf-8")
                        sys.stdout.write(data + "\n")
                        sys.stdout.flush()
                        
            except Exception as e:
                print(f"Initialization POST Error: {e}", file=sys.stderr)
                if hasattr(e, "read"):
                    try:
                        print(f"Response: {e.read().decode('utf-8')}", file=sys.stderr)
                    except Exception:
                        pass
                sys.exit(1)
                
            if not session_id:
                print("Failed to negotiate session ID from server.", file=sys.stderr)
                sys.exit(1)
                
            # Start background thread to listen to the SSE GET stream
            sse_thread = threading.Thread(
                target=sse_stream_listener,
                args=(url, base_headers, session_id),
                daemon=True
            )
            sse_thread.start()
            
        else:
            # Subsequent request: POST with session-id
            req_headers = {
                **base_headers,
                "Content-Type": "application/json",
                "mcp-session-id": session_id
            }
            req = urllib.request.Request(
                url,
                data=line.encode("utf-8"),
                headers=req_headers,
                method="POST"
            )
            try:
                with urllib.request.urlopen(req) as response:
                    content_type = response.headers.get("Content-Type", "").lower()
                    if "application/json" in content_type:
                        data = response.read().decode("utf-8")
                        if data:
                            sys.stdout.write(data + "\n")
                            sys.stdout.flush()
                    else:
                        response.read()
            except Exception as e:
                print(f"POST request error: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
