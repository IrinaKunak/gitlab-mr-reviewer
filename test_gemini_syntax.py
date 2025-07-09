#!/usr/bin/env python3

import subprocess
import tempfile
import os

# Create a simple test file
test_content = """
This is a test file.
Please review this code:

def test_function():
    print("hello world")
    return True
"""

with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
    f.write(test_content)
    test_file = f.name

try:
    # Test different gemini command formats
    print("Testing gemini command formats...")
    
    # Format 1: Just the file
    print("\n1. Testing: gemini -m gemini-2.0-flash [file]")
    result = subprocess.run(['gemini', '-m', 'gemini-2.0-flash', test_file], 
                          capture_output=True, text=True, timeout=10)
    print(f"Exit code: {result.returncode}")
    print(f"Stdout: {result.stdout[:200]}...")
    print(f"Stderr: {result.stderr[:200]}...")
    
    # Format 2: With -p flag
    print("\n2. Testing: gemini -m gemini-2.0-flash -p 'Review this' [file]")
    result = subprocess.run(['gemini', '-m', 'gemini-2.0-flash', '-p', 'Review this code:', test_file], 
                          capture_output=True, text=True, timeout=10)
    print(f"Exit code: {result.returncode}")
    print(f"Stdout: {result.stdout[:200]}...")
    print(f"Stderr: {result.stderr[:200]}...")
    
    # Format 3: Via stdin
    print("\n3. Testing: gemini -m gemini-2.0-flash -p 'Review this' < [file]")
    with open(test_file, 'r') as f:
        result = subprocess.run(['gemini', '-m', 'gemini-2.0-flash', '-p', 'Review this code:'], 
                              input=f.read(), capture_output=True, text=True, timeout=10)
    print(f"Exit code: {result.returncode}")
    print(f"Stdout: {result.stdout[:200]}...")
    print(f"Stderr: {result.stderr[:200]}...")
    
except subprocess.TimeoutExpired:
    print("Command timed out")
except Exception as e:
    print(f"Error: {e}")
finally:
    os.unlink(test_file)