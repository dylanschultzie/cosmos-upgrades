import requests
from urllib3.exceptions import InsecureRequestWarning
import re
from datetime import datetime
from random import shuffle
import json
import logging
from flask import Flask, jsonify, request
from concurrent.futures import ThreadPoolExecutor


app = Flask(__name__)

# Logging configuration
logging.basicConfig(filename='app.log', level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s')

# Suppress only the single InsecureRequestWarning from urllib3
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

BASE_URL = "https://raw.githubusercontent.com/cosmos/chain-registry/master"


MAINNET_BASE_URL = "https://raw.githubusercontent.com/cosmos/chain-registry/master"
TESTNET_BASE_URL = "https://raw.githubusercontent.com/cosmos/chain-registry/master/testnets"

SEMANTIC_VERSION_PATTERN = re.compile(r'v(\d+(?:\.\d+){0,2})')

# these servers have given consistent error responses, this list is used to skip them
SERVER_BLACKLIST = ["https://stride.api.bccnodes.com:443", "https://api.omniflix.nodestake.top", "https://cosmos-lcd.quickapi.com:443"]

def get_healthy_rpc_endpoints(rpc_endpoints):
    with ThreadPoolExecutor(max_workers=10) as executor:
        healthy_rpc_endpoints = [rpc for rpc, is_healthy in executor.map(lambda rpc: (rpc, is_endpoint_healthy(rpc['address'])), rpc_endpoints) if is_healthy]

    return healthy_rpc_endpoints[:5]  # Select the first 5 healthy RPC endpoints

def get_healthy_rest_endpoints(rest_endpoints):
    with ThreadPoolExecutor(max_workers=10) as executor:
        healthy_rest_endpoints = [rest for rest, is_healthy in executor.map(lambda rest: (rest, is_endpoint_healthy(rest['address'])), rest_endpoints) if is_healthy]

    return healthy_rest_endpoints[:5]  # Select the first 5 healthy REST endpoints

def is_endpoint_healthy(endpoint):
    try:
        response = requests.get(f"{endpoint}/health", timeout=3, verify=False)
        return response.status_code == 200
    except:
        return False

def get_healthy_endpoints(endpoints):
    healthy_endpoints = []

    def check_endpoint(endpoint):
        if is_endpoint_healthy(endpoint['address']):
            healthy_endpoints.append(endpoint)

    with ThreadPoolExecutor(max_workers=10) as executor:
        executor.map(check_endpoint, endpoints)

    return healthy_endpoints

def check_rest_endpoint(rest_url):
    """Check the REST endpoint and return the application version and response time."""
    start_time = datetime.now()
    try:
        response = requests.get(f"{rest_url}/node_info", timeout=3, verify=False)
        response.raise_for_status()
        elapsed_time = (datetime.now() - start_time).total_seconds()

        data = response.json()
        app_version = data.get('application_version', {}).get('version')
        return app_version, elapsed_time
    except (requests.RequestException, requests.Timeout):
        return None, (datetime.now() - start_time).total_seconds()

def get_latest_block_height_rpc(rpc_url):
    """Fetch the latest block height from the RPC endpoint."""
    try:
        response = requests.get(f"{rpc_url}/status", timeout=3)
        response.raise_for_status()
        data = response.json()
        return int(data.get('result', {}).get('sync_info', {}).get('latest_block_height', 0))
    except requests.RequestException as e:
        return -1  # Return -1 to indicate an error

def fetch_all_endpoints(network_type, base_url, request_data):
    """Fetch all the REST and RPC endpoints for all networks and store in a map."""
    networks = request_data.get("MAINNETS", []) if network_type == "mainnet" else request_data.get("TESTNETS", [])
    endpoints_map = {}
    for network in networks:
        rest_endpoints, rpc_endpoints = fetch_endpoints(network, base_url)
        endpoints_map[network] = {
            "rest": rest_endpoints,
            "rpc": rpc_endpoints
        }
    return endpoints_map

def fetch_endpoints(network, base_url):
    """Fetch the REST and RPC endpoints for a given network."""
    try:
        response = requests.get(f"{base_url}/{network}/chain.json")
        print(f"{base_url}/{network}/chain.json")
        response.raise_for_status()
        data = response.json()
        rest_endpoints = data.get("apis", {}).get("rest", [])
        rpc_endpoints = data.get("apis", {}).get("rpc", [])
        return rest_endpoints, rpc_endpoints
    except requests.RequestException:
        return [], []

def fetch_active_upgrade_proposals(rest_url):
    try:
        response = requests.get(f"{rest_url}/cosmos/gov/v1beta1/proposals?proposal_status=2", verify=False)

        # Handle 501 Server Error
        if response.status_code == 501:
            return None, None

        response.raise_for_status()
        data = response.json()

        for proposal in data.get("proposals", []):
            content = proposal.get("content", {})
            if content.get("@type") == "/cosmos.upgrade.v1beta1.SoftwareUpgradeProposal":
                # Extract version from the plan name
                plan_name = content.get("plan", {}).get("name", "")
                plan_version_match = SEMANTIC_VERSION_PATTERN.search(plan_name)

                # Extract version from the description
                description = content.get("description", "")
                description_version_match = SEMANTIC_VERSION_PATTERN.search(description)

                # Prioritize the longer semantic version

                if plan_version_match and description_version_match:
                    version = plan_version_match.group(1) if len(plan_version_match.group(1)) > len(description_version_match.group(1)) else description_version_match.group(1)
                elif plan_version_match:
                    version = plan_version_match.group(1)
                elif description_version_match:
                    version = description_version_match.group(1)
                else:
                    version = None

                try:
                    height = int(content.get("plan", {}).get("height", 0))
                except ValueError:
                    height = 0

                if version:
                    return version, height
        return None, None
    except requests.RequestException as e:
        print(f"Error received from server {rest_url}: {e}")
        raise e
    except Exception as e:
        print(f"Unhandled error while requesting active upgrade endpoint from {rest_url}: {e}")
        raise e

def fetch_current_upgrade_plan(rest_url):
    try:
        response = requests.get(f"{rest_url}/cosmos/upgrade/v1beta1/current_plan", verify=False)
        response.raise_for_status()
        data = response.json()

        plan = data.get("plan", {})
        if plan:
            version_match = SEMANTIC_VERSION_PATTERN.search(plan.get("name", ""))
            if version_match:
                version = version_match.group(1)
                try:
                    height = int(plan.get("height", 0))
                except ValueError:
                    height = 0
                return version, height
        return None, None
    except requests.RequestException as e:
        print(f"Error received from server {rest_url}: {e}")
        raise e
    except Exception as e:
        print(f"Unhandled error while requesting current upgrade endpoint from {rest_url}: {e}")
        raise e

def fetch_data_for_network(network, endpoints, network_type):

    print(f"Fetching data for network {network}")
    rest_endpoints = endpoints.get("rest", [])
    rpc_endpoints = endpoints.get("rpc", [])
    print(f"Found {len(rest_endpoints)} rest endpoints and {len(rpc_endpoints)} rpc endpoints")

    # Prioritize RPC endpoints for fetching the latest block height
    # Shuffle RPC endpoints to avoid calling the same one over and over
    latest_block_height = -1
    healthy_rpc_endpoints = get_healthy_rpc_endpoints(rpc_endpoints)
    healthy_rest_endpoints = get_healthy_rest_endpoints(rest_endpoints)

    # Shuffle the healthy endpoints
    shuffle(healthy_rpc_endpoints)
    shuffle(healthy_rest_endpoints)

    rpc_server_used = ""
    for rpc_endpoint in healthy_rpc_endpoints:
        latest_block_height = get_latest_block_height_rpc(rpc_endpoint['address'])
        if latest_block_height > 0:
            rpc_server_used = rpc_endpoint['address']
            break

    # Check for active upgrade proposals
    # Shuffle RPC endpoints to avoid calling the same one over and over
    shuffle(healthy_rest_endpoints)
    upgrade_block_height = None
    upgrade_version = ""
    source = ""

    for index, rest_endpoint in enumerate(healthy_rest_endpoints):
        current_endpoint = rest_endpoint["address"]

        if current_endpoint in SERVER_BLACKLIST:
            continue
        try:
            active_upgrade_version, active_upgrade_height = fetch_active_upgrade_proposals(current_endpoint)
            current_upgrade_version, current_upgrade_height = fetch_current_upgrade_plan(current_endpoint)
        except:
            if index + 1 < len(healthy_rest_endpoints):
                print(f"Failed to query rest endpoints {current_endpoint}, trying next rest endpoint")
                continue
            else:
                print(f"Failed to query rest endpoints {current_endpoint}, all out of endpoints to try")
                break

        if active_upgrade_version and (active_upgrade_height is not None) and active_upgrade_height > latest_block_height:
            upgrade_block_height = active_upgrade_height
            upgrade_version = active_upgrade_version
            source = "active_upgrade_proposals"
            break

        if current_upgrade_version and (current_upgrade_height is not None) and current_upgrade_height > latest_block_height:
            upgrade_block_height = current_upgrade_height
            upgrade_version = current_upgrade_version
            source = "current_upgrade_plan"
            break

    output_data = {
        "type": network_type,
        "network": network,
        "upgrade_found": upgrade_version != "",
        "latest_block_height": latest_block_height,
        "upgrade_block_height": upgrade_block_height,
        "version": upgrade_version,
        "rpc_server": rpc_server_used,
        "source": source
    }
    return output_data

@app.route('/fetch', methods=['POST'])
def fetch_network_data():
    try:
        request_data = request.get_json()
        if not request_data:
            return jsonify({"error": "Invalid payload"}), 400

        results = []
        for network_type, base_url, networks in [("mainnet", BASE_URL, request_data.get("MAINNETS", [])),
                                                 ("testnet", BASE_URL + "/testnets", request_data.get("TESTNETS", []))]:
            endpoints_map = fetch_all_endpoints(network_type, base_url, request_data)
            for network in networks:
                try:
                    network_data = fetch_data_for_network(network, endpoints_map.get(network, {}), network_type)
                    results.append(network_data)
                except Exception as e:
                    logging.error(f"Error fetching data for network {network}: {e}")

        # Sort the results by 'upgrade_found' in descending order (chain upgrades first)
        sorted_results = sorted(results, key=lambda x: x['upgrade_found'], reverse=True)

        return jsonify(sorted_results)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True)
