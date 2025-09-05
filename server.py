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
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime

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

# GraphQL endpoint for project information
GRAPHQL_URL = "https://strapi-dev.scand.app/graphql"

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


async def get_project_info_from_graphql(package_id: str) -> Optional[Dict[str, Any]]:
    """
    Get project information from GraphQL API using a package ID.
    
    Args:
        package_id: The Sui package ID
        
    Returns:
        Project information or None if not found
    """
    try:
        query = """
        fragment projectEntity on ProjectEntity {
          attributes {
            ProjectName
            publishedAt
            SubmitterAddress
            ProjectWebsite
            ProjectWhitepaper
            ProjectGithub
            ProjectImage {
              data {
                attributes {
                  url
                  __typename
                }
                __typename
              }
              __typename
            }
            FullDescription
            ShortDescription
            DexLink
            PoolLink
            email
            linkedin
            discord
            twitter
            telegram
            medium
            mirror
            facebook
            wechat
            link3
            reddit
            slack
            categories(pagination: {start: 0, limit: -1}) {
              data {
                attributes {
                  Category
                  __typename
                }
                __typename
              }
              __typename
            }
            ImgSlider {
              data {
                attributes {
                  Files {
                    data {
                      attributes {
                        url
                        name
                        __typename
                      }
                      __typename
                    }
                    __typename
                  }
                  isEnabled
                  __typename
                }
                __typename
              }
              __typename
            }
            BackgroundImage {
              data {
                attributes {
                  url
                  __typename
                }
                __typename
              }
              __typename
            }
            categories_related {
              data {
                attributes {
                  Category
                  __typename
                }
                __typename
              }
              __typename
            }
            tokens(pagination: {start: 0, limit: -1}) {
              data {
                attributes {
                  TokenId
                  TokenName
                  TokenLabel
                  __typename
                }
                __typename
              }
              __typename
            }
            contracts(pagination: {start: 0, limit: -1}) {
              data {
                attributes {
                  ContractId
                  ContractLabel
                  ContractName
                  __typename
                }
                __typename
              }
              __typename
            }
            chains {
              data {
                attributes {
                  ChainName
                  __typename
                }
                __typename
              }
              __typename
            }
            __typename
          }
          __typename
        }

        query package($hash: String) {
          contracts(filters: {ContractId: {eq: $hash}, chain: {ChainName: {eq: "Sui"}}}) {
            data {
              attributes {
                ContractName
                ContractId
                project {
                  data {
                    ...projectEntity
                    __typename
                  }
                  __typename
                }
                __typename
              }
              __typename
            }
            __typename
          }
        }
        """

        payload = {
            "operationName": "package",
            "variables": {"hash": package_id},
            "query": query
        }

        headers = {
            'accept': '*/*',
            'accept-language': 'en-GB,en-US;q=0.9,en;q=0.8',
            'content-type': 'application/json',
            'origin': 'https://suiscan.xyz',
            'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36'
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                GRAPHQL_URL,
                headers=headers,
                json=payload
            ) as response:
                if response.status != 200:
                    logger.error(f"GraphQL HTTP error {response.status}: {await response.text()}")
                    return None
                
                data = await response.json()
                
                if "errors" in data:
                    logger.error(f"GraphQL errors: {data['errors']}")
                    return None
                
                contracts = data.get("data", {}).get("contracts", {}).get("data", [])
                if not contracts:
                    logger.warning(f"No project found for package ID: {package_id}")
                    return None
                
                return contracts[0].get("attributes", {}).get("project", {}).get("data", {})

    except Exception as e:
        logger.error(f"Error fetching project info from GraphQL: {e}")
        return None


async def get_package_transactions(package_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    """
    Get transaction history for a package to determine update times and versions.
    
    Args:
        package_id: The Sui package ID
        limit: Maximum number of transactions to fetch
        
    Returns:
        List of transaction data
    """
    try:
        rpc_payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "suix_queryTransactionBlocks",
            "params": [
                {
                    "filter": {
                        "ChangedObject": package_id
                    },
                    "options": {
                        "showEffects": True,
                        "showBalanceChanges": True,
                        "showInput": True
                    }
                },
                None,
                limit,
                True  # descending order
            ]
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                SUI_RPC_URL,
                headers={"Content-Type": "application/json"},
                json=rpc_payload
            ) as response:
                if response.status != 200:
                    logger.error(f"Transaction query HTTP error {response.status}: {await response.text()}")
                    return []
                
                data = await response.json()
                
                if "error" in data:
                    logger.error(f"Transaction query RPC error: {data['error']}")
                    return []
                
                return data.get("result", {}).get("data", [])

    except Exception as e:
        logger.error(f"Error fetching package transactions: {e}")
        return []


async def get_package_modules(package_id: str) -> List[str]:
    """
    Get list of modules in a package.
    
    Args:
        package_id: The Sui package ID
        
    Returns:
        List of module names
    """
    try:
        module_bytecode = await download_package_bytecode(package_id)
        return list(module_bytecode.keys())
    except Exception as e:
        logger.error(f"Error getting package modules: {e}")
        return []


async def get_package_info_detailed(package_id: str, project_contracts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Get detailed information about a specific package including modules and update history.
    
    Args:
        package_id: The Sui package ID
        project_contracts: List of all contracts in the project
        
    Returns:
        Package information with modules and update history
    """
    try:
        # Find this package in the project contracts list
        package_info = None
        for contract in project_contracts:
            if contract.get("ContractId") == package_id:
                package_info = contract
                break
        
        if not package_info:
            package_info = {
                "ContractId": package_id,
                "ContractName": f"Package {package_id[:8]}...",
                "ContractLabel": "Package"
            }

        # Get modules in this package
        modules = await get_package_modules(package_id)
        
        # Get transaction history to determine last update time and version
        transactions = await get_package_transactions(package_id, 50)
        
        last_update_time = None
        version = None
        
        if transactions:
            # Most recent transaction (first in descending order)
            latest_tx = transactions[0]
            timestamp_ms = latest_tx.get("timestampMs")
            
            if timestamp_ms:
                last_update_time = datetime.fromtimestamp(int(timestamp_ms) / 1000).isoformat()
            
            # Look for version in effects
            effects = latest_tx.get("effects", {})
            created = effects.get("created", [])
            
            for created_obj in created:
                if created_obj.get("reference", {}).get("objectId") == package_id:
                    version = created_obj.get("reference", {}).get("version")
                    break

        return {
            "package_id": package_id,
            "name": package_info.get("ContractName", "Unknown"),
            "label": package_info.get("ContractLabel", "Package"),
            "modules": modules,
            "module_count": len(modules),
            "last_update_time": last_update_time,
            "version": version,
            "transaction_count": len(transactions)
        }

    except Exception as e:
        logger.error(f"Error getting detailed package info for {package_id}: {e}")
        return {
            "package_id": package_id,
            "name": f"Package {package_id[:8]}...",
            "label": "Package",
            "modules": [],
            "module_count": 0,
            "last_update_time": None,
            "version": None,
            "transaction_count": 0,
            "error": str(e)
        }


@mcp.tool()
async def get_project_info(package_id: str) -> dict:
    """
    Get project information including all packages, modules, and version history.
    Takes one of the project's package IDs and returns complete project information
    with all packages sorted by their last change time.
    
    Args:
        package_id: One of the project's package IDs (hex string starting with 0x)
        
    Returns:
        dict: Complete project information with packages, modules, and version history
    """
    try:
        logger.info(f"Getting project info for package {package_id}")
        
        # Step 1: Get project information from GraphQL
        project_data = await get_project_info_from_graphql(package_id)
        
        if not project_data:
            return {
                "success": False,
                "error": f"No project found for package ID: {package_id}",
                "package_id": package_id
            }
        
        project_attrs = project_data.get("attributes", {})
        
        # Step 2: Extract all contracts/packages from the project
        contracts_data = project_attrs.get("contracts", {}).get("data", [])
        package_ids = []
        
        for contract in contracts_data:
            contract_attrs = contract.get("attributes", {})
            contract_id = contract_attrs.get("ContractId")
            if contract_id and contract_attrs.get("ContractLabel") == "Package":
                package_ids.append(contract_attrs)
        
        logger.info(f"Found {len(package_ids)} packages in project")
        
        # Step 3: Get detailed information for each package
        package_details = []
        for contract_info in package_ids:
            pkg_id = contract_info.get("ContractId")
            if pkg_id:
                package_detail = await get_package_info_detailed(pkg_id, contracts_data)
                package_details.append(package_detail)
        
        # Step 4: Sort packages by last update time (most recent first)
        def get_update_time(pkg):
            update_time = pkg.get("last_update_time")
            if update_time:
                try:
                    return datetime.fromisoformat(update_time)
                except:
                    return datetime.min
            return datetime.min
        
        package_details.sort(key=get_update_time, reverse=True)
        
        # Step 5: Build the complete response
        result = {
            "success": True,
            "query_package_id": package_id,
            "project": {
                "name": project_attrs.get("ProjectName", "Unknown Project"),
                "description_short": project_attrs.get("ShortDescription", ""),
                "description_full": project_attrs.get("FullDescription", ""),
                "website": project_attrs.get("ProjectWebsite", ""),
                "github": project_attrs.get("ProjectGithub", ""),
                "published_at": project_attrs.get("publishedAt", ""),
                "social_links": {
                    "discord": project_attrs.get("discord", ""),
                    "twitter": project_attrs.get("twitter", ""),
                    "telegram": project_attrs.get("telegram", ""),
                    "medium": project_attrs.get("medium", ""),
                    "email": project_attrs.get("email", "")
                },
                "categories": [
                    cat.get("attributes", {}).get("Category", "") 
                    for cat in project_attrs.get("categories", {}).get("data", [])
                ]
            },
            "packages": package_details,
            "package_count": len(package_details),
            "total_modules": sum(pkg.get("module_count", 0) for pkg in package_details),
            "tokens": [
                {
                    "id": token.get("attributes", {}).get("TokenId", ""),
                    "name": token.get("attributes", {}).get("TokenName", ""),
                    "label": token.get("attributes", {}).get("TokenLabel", "")
                }
                for token in project_attrs.get("tokens", {}).get("data", [])
            ]
        }
        
        logger.info(f"Successfully retrieved project info: {result['project']['name']} with {result['package_count']} packages")
        return result

    except Exception as e:
        logger.error(f"Error in get_project_info: {e}")
        return {
            "success": False,
            "error": f"Failed to get project info: {str(e)}",
            "package_id": package_id
        }


if __name__ == "__main__":
    # Run the MCP server
    mcp.run()