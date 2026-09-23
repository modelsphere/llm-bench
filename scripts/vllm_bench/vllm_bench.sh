#!/usr/bin/env bash
# Standalone vLLM endpoint benchmark — assumes vllm is installed locally.
#
# 1. Edit the CONFIG variables below, then run:
#      ./vllm_bench.sh
# 2. Or override any value via flags:
#      ./vllm_bench.sh --output-len 32000 --max-concurrency 50

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG — edit these four lines
# ═══════════════════════════════════════════════════════════════════════════════
BASE_URL="https://api.qnaigc.com"
API_KEY="sk-f526aa037013671ece0182c19d61fbcf16870d234717a96c0b20cc1de7f1da36"
TOKENIZER="Qwen/Qwen3-0.6B"   # HF repo id, or a local tokenizer directory
MODEL="moonshotai/kimi-k2.5"
# ═══════════════════════════════════════════════════════════════════════════════

# Tunable defaults
DATASET_NAME="random"
INPUT_LEN=50000
OUTPUT_LEN=1500
NUM_PROMPTS=200
MAX_CONCURRENCY=20
ENDPOINT="/v1/chat/completions"
IGNORE_EOS="--ignore-eos"
TRUST_REMOTE_CODE="--trust-remote-code"

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Options (override the CONFIG section at the top of this script):
  --base-url URL          API base URL
  --model NAME            Model name
  --tokenizer PATH        Path to local tokenizer / processor
  --api-key KEY           Bearer API key
  --dataset-name NAME     Dataset: random (default: random)
  --input-len N           Input prompt length in tokens (default: 50)
  --output-len N          Max output tokens per request (default: 64000)
  --num-prompts N         Total prompts to send (default: 200)
  --max-concurrency N     Max concurrent requests (default: 100)
  --endpoint PATH         API endpoint path (default: /v1/completions)
  --ignore-eos            Pass ignore_eos flag
  --no-trust-remote-code  Disable trust-remote-code
  -h, --help              Show this help

EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --base-url) BASE_URL="$2"; shift 2 ;;
        --model) MODEL="$2"; shift 2 ;;
        --tokenizer) TOKENIZER="$2"; shift 2 ;;
        --api-key) API_KEY="$2"; shift 2 ;;
        --dataset-name) DATASET_NAME="$2"; shift 2 ;;
        --input-len) INPUT_LEN="$2"; shift 2 ;;
        --output-len) OUTPUT_LEN="$2"; shift 2 ;;
        --num-prompts) NUM_PROMPTS="$2"; shift 2 ;;
        --max-concurrency) MAX_CONCURRENCY="$2"; shift 2 ;;
        --endpoint) ENDPOINT="$2"; shift 2 ;;
        --ignore-eos) IGNORE_EOS="--ignore-eos"; shift ;;
        --no-trust-remote-code) TRUST_REMOTE_CODE=""; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
    esac
done

if [[ -z "$BASE_URL" || -z "$MODEL" || -z "$TOKENIZER" ]]; then
    echo "Error: BASE_URL, MODEL, and TOKENIZER must be set in the CONFIG section or via flags." >&2
    usage
    exit 1
fi

if ! command -v vllm &> /dev/null; then
    echo "Error: vllm CLI not found. Install with: pip install vllm" >&2
    exit 1
fi

# Resolve tokenizer path relative to repo root if not absolute
if [[ ! "$TOKENIZER" = /* ]]; then
    TOKENIZER="${REPO_ROOT}/${TOKENIZER}"
fi

if [[ ! -d "$TOKENIZER" ]]; then
    echo "Error: tokenizer directory not found: $TOKENIZER" >&2
    exit 1
fi

export HF_HUB_OFFLINE=1
if [[ -n "$API_KEY" ]]; then
    export OPENAI_API_KEY="$API_KEY"
fi

echo "Running vllm bench serve..."
echo "  base-url:        $BASE_URL"
echo "  model:           $MODEL"
echo "  tokenizer:       $TOKENIZER"
echo "  dataset:         $DATASET_NAME"
echo "  input-len:       $INPUT_LEN"
echo "  output-len:      $OUTPUT_LEN"
echo "  num-prompts:     $NUM_PROMPTS"
echo "  max-concurrency: $MAX_CONCURRENCY"
echo "  endpoint:        $ENDPOINT"
echo ""

vllm bench serve \
    --backend openai \
    --base-url "$BASE_URL" \
    --model "$MODEL" \
    --tokenizer "$TOKENIZER" \
    $TRUST_REMOTE_CODE \
    --dataset-name "$DATASET_NAME" \
    --input-len "$INPUT_LEN" \
    --output-len "$OUTPUT_LEN" \
    --num-prompts "$NUM_PROMPTS" \
    --max-concurrency "$MAX_CONCURRENCY" \
    --endpoint "$ENDPOINT" \
    $IGNORE_EOS
