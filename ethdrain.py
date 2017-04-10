#!/usr/bin/python3
import asyncio
import json
import datetime
import logging
import multiprocessing as mp
import argparse
import requests
import aiohttp

from elasticsearch import Elasticsearch
from elasticsearch import helpers

logging.basicConfig(filename='error_blocks.log', level=logging.ERROR)

TX_INDEX_NAME = "ethereum-transaction"
B_INDEX_NAME = "ethereum-block"
WEI_ETH_FACTOR = 1000000000000000000.0

# Elasticsearch maximum number of connections
ES_MAXSIZE = 25
# Elasticsearch default url
ES_URL = "http://localhost:9200"
# Ethereum RPC endpoint
ETH_URL = "http://localhost:8545"
# Parallel processing semaphore size
SEM_SIZE = 256
# Size of chunk size in blocks
CHUNK_SIZE = 250
# Size of multiprocessing Pool processing the chunks
POOL_SIZE = mp.cpu_count() + 2


def chunks(lst, nb_chunks):
    for i in range(0, len(lst), nb_chunks):
        yield lst[i:i + nb_chunks]


def make_request(block, use_hex=True):
    return json.dumps({
        "jsonrpc": "2.0",
        "method": "eth_getBlockByNumber",
        "params": [hex(block) if use_hex else block, True],
        "id": 1
    })

def post_request(url, request):
    return requests.post(url, data=request, headers={"content-type": "application/json"}).json()

def es_request(url, **kwargs):
    elasticsearch = Elasticsearch([url])
    return elasticsearch.search(**kwargs)

async def fetch(url, session, block, process_fn, actions):
    try:
        async with session.post(url, data=make_request(block), headers={"content-type": "application/json"}) as response:
            process_fn(await response.json(), actions)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exception:
        logging.error("block: " + str(block))
        print("Issue with block {}:\n{}\n".format(block, exception))


async def sema_fetch(sem, url, session, block, process_fn, actions):
    async with sem:
        await fetch(url, session, block, process_fn, actions)


async def run(block_range, process_fn, actions):
    tasks = []
    sem = asyncio.Semaphore(SEM_SIZE)

    # Create client session that will ensure we dont open new connection
    # per each request.
    async with aiohttp.ClientSession() as session:
        for i in block_range:
            # pass Semaphore and session to every POST request
            task = asyncio.ensure_future(sema_fetch(sem, ETH_URL, session, i, process_fn, actions))
            tasks.append(task)

        await asyncio.gather(*tasks)


def process_block(block, actions):
    block = block["result"]

    transactions = block["transactions"]
    tx_hashes = list()
    tx_value_sum = 0

    block_nb = int(block["number"], 0)
    block_timestamp = datetime.datetime.fromtimestamp(int(block["timestamp"], 0))

    for tx in transactions:
        tx["blockNumber"] = int(tx["blockNumber"], 0)
        tx["blockTimestamp"] = block_timestamp
        # Convert wei into ether
        tx["value"] = int(tx["value"], 0) / WEI_ETH_FACTOR
        tx_value_sum += tx["value"]
        actions.append(
            {"_index": TX_INDEX_NAME, "_type": "tx", "_id": tx["hash"], "_source": tx}
        )
        tx_hashes.append(tx["hash"])

    block["transactions"] = tx_hashes
    block["number"] = block_nb
    block["timestamp"] = block_timestamp
    block["gasLimit"] = int(block["gasLimit"], 0)
    block["gasUsed"] = int(block["gasUsed"], 0)
    block["size"] = int(block["size"], 0)
    block["transactionCount"] = len(tx_hashes)
    block["txValueSum"] = tx_value_sum

    actions.append({"_index": B_INDEX_NAME, "_type": "b", "_id": block_nb, "_source": block})


def setup_process(block_range):
    out_actions = list()

    elasticsearch = Elasticsearch([ES_URL], maxsize=ES_MAXSIZE, timeout=30, max_retries=10, retry_on_timeout=True)

    loop = asyncio.get_event_loop()
    future = asyncio.ensure_future(run(block_range, process_block, out_actions))
    loop.run_until_complete(future)

    blocks = [act for act in out_actions if act["_type"] == "b"]
    txs = [act for act in out_actions if act["_type"] == "tx"]

    if blocks or txs:
        try:
            helpers.bulk(elasticsearch, out_actions)
            print("#{}: ({}b, {}tx)".format(
                max([int(b["_id"]) for b in blocks]), len(blocks), len(txs)
            ))
        except helpers.BulkIndexError as exception:
            print("Issue with {} blocks:\n{}\n".format(len(blocks), exception))
            for act in blocks:
                logging.error("block: " + str(act["_id"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument('-s', '--start', dest='start_block', type=int,
                        help='What block to start indexing. If nothing is provided, the lastest block indexed in ElasticSearch will be used.')
    parser.add_argument('-e', '--end', dest='end_block', type=int,
                        help='What block to finish indexing. If nothing is provided, the latest one will be used.')
    parser.add_argument('-f', '--file', default=None,
                        help='Use an input file, each block number on a new line.')
    parser.add_argument('-u', '--esurl', default=ES_URL,
                        help='The elasticsearch url and port. Accepts all the same parameters needed as a normal Elasticsearch client expects.')
    parser.add_argument('-m', '--esmaxsize', default=ES_MAXSIZE, help='The elasticsearch max chunk size.')
    parser.add_argument('-r', '--ethrpcurl', default=ETH_URL, help='The Ethereum RPC node url and port.')
    args = parser.parse_args()

    # Determine start block number if needed
    if not args.start_block:
        args.start_block = es_request(ES_URL, index=B_INDEX_NAME, size=1, sort="number:desc")["hits"]["hits"][0]["_source"]["number"]
        print("Start block automatically set to: {}".format(args.start_block))

    # Determine last block number if needed
    if not args.end_block:
        args.end_block = int(post_request(ETH_URL, make_request("latest", False))["result"]["number"], 0)
        print("Last block automatically set to: {}".format(args.end_block))

    if args.file:
        with open(args.file) as f:
            CONTENT = f.readlines()
            block_list = [int(x) for x in CONTENT if x.strip() and len(x.strip()) <= 8]
    else:
        block_list = list(range(int(args.start_block), int(args.end_block)))

    ES_MAXSIZE = int(args.esmaxsize)
    ES_URL = args.esurl
    ETH_URL = args.ethrpcurl

    chunks_arr = list(chunks(block_list, CHUNK_SIZE))

    print("~~Processing {} blocks split into {} chunks~~\n".format(
        len(block_list), len(chunks_arr)
    ))

    POOL = mp.Pool(POOL_SIZE)
    POOL.map(setup_process, chunks_arr)
