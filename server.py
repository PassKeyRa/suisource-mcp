#!/usr/bin/env python3
import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Any

import aiohttp
from fastmcp import FastMCP

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastMCP server
mcp = FastMCP("Sui Source Code Decompiler")

# Default Sui RPC endpoint
SUI_RPC_URL = os.getenv("SUI_RPC_URL", "https://fullnode.mainnet.sui.io/")
WORKDIR = os.getenv("WORKDIR", "/workdir")

@mcp.tool()
async def health_check() -> dict:
    """
    Check if the server and revela binary are working correctly.
    
    Returns:
        dict: Health status information
    """
    try:
        # Check if revela binary is available
        result = subprocess.run(
            ["revela", "--help"],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        revela_available = result.returncode == 0
        
        return {
            "status": "healthy" if revela_available else "unhealthy",
            "revela_available": revela_available,
            "sui_rpc_url": SUI_RPC_URL,
            "server": "suisource-mcp",
            "version": "1.0.0"
        }
        
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
            "revela_available": False,
            "sui_rpc_url": SUI_RPC_URL,
            "server": "suisource-mcp",
            "version": "1.0.0"
        }


async def _get_source_code_impl(package_id: str) -> dict:
    """
    Download bytecode for a Sui package ID and decompile it using revela.
    
    Args:
        package_id: The Sui package ID (hex string starting with 0x)
        
    Returns:
        dict: Status and details of the decompilation process
    """
    try:
        # Create output directory if it doesn't exist
        output_dir = Path(WORKDIR)
        output_dir.mkdir(parents=True, exist_ok=True)
        for filename in os.listdir(WORKDIR):
            file_path = os.path.join(WORKDIR, filename)
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
            except Exception as e:
                logger.error('Failed to delete %s. Reason: %s' % (file_path, e))
        
        logger.info(f"Starting decompilation for package {package_id}")
        
        # Step 1: Download bytecode from Sui RPC
        logger.info("Downloading bytecode from Sui RPC...")
        module_bytecode = await download_package_bytecode(package_id)
        
        if not module_bytecode:
            return {
                "success": False,
                "error": "Failed to download bytecode - package not found or no modules"
            }
        
        logger.info(f"Downloaded {len(module_bytecode)} modules")
        
        # Step 2: Create temporary directory for bytecode files
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            
            # Step 3: Decode and save bytecode files
            bytecode_files = []
            for module_name, base64_bytecode in module_bytecode.items():
                try:
                    # Decode base64 bytecode
                    bytecode = base64.b64decode(base64_bytecode)
                    
                    # Save to temporary file
                    bytecode_file = temp_path / f"{module_name}.bytecode"
                    with open(bytecode_file, 'wb') as f:
                        f.write(bytecode)
                    
                    bytecode_files.append((module_name, bytecode_file))
                    logger.info(f"Saved bytecode for module: {module_name}")
                    
                except Exception as e:
                    logger.error(f"Failed to decode bytecode for module {module_name}: {e}")
                    continue
            
            # Step 4: Decompile each module using revela
            decompiled_modules = []
            failed_modules = []
            
            for module_name, bytecode_file in bytecode_files:
                try:
                    logger.info(f"Decompiling module: {module_name}")
                    source_code = await decompile_with_revela(bytecode_file)
                    
                    if source_code:
                        # Save source code to output directory
                        source_file = output_dir / f"{module_name}.move"
                        with open(source_file, 'w', encoding='utf-8') as f:
                            f.write(source_code)
                        
                        decompiled_modules.append(module_name)
                        logger.info(f"Successfully decompiled and saved: {module_name}.move")
                    else:
                        failed_modules.append(module_name)
                        logger.error(f"Failed to decompile module: {module_name}")
                        
                except Exception as e:
                    failed_modules.append(module_name)
                    logger.error(f"Error decompiling module {module_name}: {e}")
        
        # Step 5: Return results
        result = {
            "success": True,
            "package_id": package_id,
            "output_dir_info": "The container's workdir is used to save the sources, which is typically the local /tmp/suisource-mcp mounted dir. All sources are saved with .move extension without subdirs directly in the mounted dir. Move sources to the target specified directory if needed",
            "total_modules": len(module_bytecode),
            "decompiled_modules": decompiled_modules,
            "failed_modules": failed_modules,
            "decompiled_count": len(decompiled_modules),
            "failed_count": len(failed_modules),
        }
        
        logger.info(f"Decompilation completed: {len(decompiled_modules)}/{len(module_bytecode)} modules successful")
        return result
        
    except Exception as e:
        logger.error(f"Error in get_source_code: {e}")
        return {
            "success": False,
            "error": f"Decompilation failed: {str(e)}"
        }


async def download_package_bytecode(package_id: str) -> Dict[str, str]:
    """
    Download bytecode for all modules in a Sui package.
    
    Args:
        package_id: The Sui package ID
        
    Returns:
        Dict mapping module names to base64-encoded bytecode
    """
    try:
        # Prepare RPC request
        rpc_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sui_getObject",
            "params": [
                package_id,
                {
                    "showType": True,
                    "showOwner": True,
                    "showPreviousTransaction": True,
                    "showDisplay": True,
                    "showContent": True,
                    "showBcs": True,
                    "showStorageRebate": True
                }
            ]
        }
        
        # Make HTTP request to Sui RPC
        async with aiohttp.ClientSession() as session:
            async with session.post(
                SUI_RPC_URL,
                headers={"Content-Type": "application/json"},
                json=rpc_payload
            ) as response:
                if response.status != 200:
                    logger.error(f"HTTP error {response.status}: {await response.text()}")
                    return {}
                
                data = await response.json()
                
                # Check for RPC errors
                if "error" in data:
                    logger.error(f"RPC error: {data['error']}")
                    return {}
                
                # Extract module map from response
                result = data.get("result", {})
                bcs_data = result.get("data", {}).get("bcs", {})
                module_map = bcs_data.get("moduleMap", {})
                
                if not module_map:
                    logger.warning("No moduleMap found in response")
                    return {}
                
                logger.info(f"Retrieved bytecode for {len(module_map)} modules")
                return module_map
                
    except Exception as e:
        logger.error(f"Error downloading bytecode: {e}")
        return {}


async def decompile_with_revela(bytecode_file: Path) -> str:
    """
    Use revela binary to decompile bytecode file to Move source code.
    
    Args:
        bytecode_file: Path to the bytecode file
        
    Returns:
        Decompiled Move source code as string, or empty string if failed
    """
    try:
        # Run revela command
        cmd = ["revela", "-b", str(bytecode_file)]
        
        logger.debug(f"Running command: {' '.join(cmd)}")
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30  # 30 second timeout
        )
        
        if result.returncode == 0:
            return result.stdout.strip()
        else:
            logger.error(f"Revela failed with code {result.returncode}: {result.stderr}")
            return ""
            
    except subprocess.TimeoutExpired:
        logger.error(f"Revela command timed out for {bytecode_file}")
        return ""
    except FileNotFoundError:
        logger.error("Revela binary not found. Make sure it's installed and in PATH.")
        logger.info("For local testing without Revela, install it from: https://github.com/verichains/revela/releases/tag/v1.0.0")
        return ""
    except Exception as e:
        logger.error(f"Error running revela: {e}")
        return ""


@mcp.tool()
async def get_source_code(package_id: str) -> dict:
    """
    Download bytecode for a Sui package ID and decompile it using revela. The container's workdir is used to save the sources, which is typically the local /tmp/suisource-mcp mounted dir. All sources are saved with .move extension without subdirs directly in the mounted dir. Move sources to the target specified directory if needed.
    
    Args:
        package_id: The Sui package ID (hex string starting with 0x)
        
    Returns:
        dict: Status and details of the decompilation process
    """
    return await _get_source_code_impl(package_id)


if __name__ == "__main__":
    # Run the MCP server
    mcp.run()