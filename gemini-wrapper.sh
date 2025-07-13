#!/bin/bash

# Configuration
GEMINI_CACHE_DIR="${GEMINI_CACHE_DIR:-$HOME/.gitlab-mr-reviewer/cache}"
GEMINI_CACHE_TTL="${GEMINI_CACHE_TTL:-3600}"  # 1 hour
GEMINI_TIMEOUT="${GEMINI_TIMEOUT:-60}"        # 60 seconds
GEMINI_RATE_LIMIT="${GEMINI_RATE_LIMIT:-2}"   # 2 seconds between calls
GEMINI_MODEL="${GEMINI_MODEL:-gemini-2.5-flash}"

# Debug logging configuration
GEMINI_DEBUG="${GEMINI_DEBUG:-false}"
GEMINI_LOG_DIR="${GEMINI_LOG_DIR:-$HOME/.gitlab-mr-reviewer/logs}"
GEMINI_LOG_FILE="$GEMINI_LOG_DIR/gemini-debug.log"

# Load prompts from environment or use defaults
REVIEW_LANGUAGE="${REVIEW_LANGUAGE:-en}"
GEMINI_PROMPT="${GEMINI_PROMPT:-Review this code change and provide:
1. Code quality assessment
2. Potential bugs or issues
3. Security concerns
4. Performance considerations
5. Best practices violations
6. Suggestions for improvement

Be concise but thorough. Focus on actionable feedback.}"

GEMINI_PROMPT_RU="${GEMINI_PROMPT_RU:-Проанализируйте этот запрос на слияние и предоставьте:
1. Оценка качества кода
2. Потенциальные баги или проблемы
3. Проблемы безопасности
4. Вопросы производительности
5. Нарушения лучших практик
6. Предложения по улучшению

Будьте лаконичными, но основательными. Сосредоточьтесь на практических рекомендациях.}"

# Select prompt based on language
if [ "$REVIEW_LANGUAGE" = "ru" ]; then
    SELECTED_PROMPT="$GEMINI_PROMPT_RU"
else
    SELECTED_PROMPT="$GEMINI_PROMPT"
fi

# Rate limiting file
RATE_LIMIT_FILE="/tmp/gitlab_mr_gemini_last_call"

# Logging
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $1" >&2
}

error() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] ERROR: $1" >&2
}

debug() {
    if [ "${DEBUG:-false}" = "true" ]; then
        echo "[$(date +'%Y-%m-%d %H:%M:%S')] DEBUG: $1" >&2
    fi
}

# Gemini-specific debug logging
gemini_debug() {
    if [ "$GEMINI_DEBUG" = "true" ]; then
        echo "[$(date +'%Y-%m-%d %H:%M:%S')] GEMINI-DEBUG: $1" >&2
        if [ -n "$GEMINI_LOG_FILE" ]; then
            echo "[$(date +'%Y-%m-%d %H:%M:%S')] GEMINI-DEBUG: $1" >> "$GEMINI_LOG_FILE"
        fi
    fi
}

# Log request/response to separate debug file
log_gemini_request() {
    local content="$1"
    if [ "$GEMINI_DEBUG" = "true" ] && [ -n "$GEMINI_LOG_FILE" ]; then
        echo "" >> "$GEMINI_LOG_FILE"
        echo "================== GEMINI REQUEST $(date +'%Y-%m-%d %H:%M:%S') ==================" >> "$GEMINI_LOG_FILE"
        echo "MODEL: $GEMINI_MODEL" >> "$GEMINI_LOG_FILE"
        echo "CONTENT LENGTH: $(echo "$content" | wc -c) characters" >> "$GEMINI_LOG_FILE"
        echo "CONTENT:" >> "$GEMINI_LOG_FILE"
        echo "$content" >> "$GEMINI_LOG_FILE"
        echo "=================================================================" >> "$GEMINI_LOG_FILE"
    fi
}

log_gemini_response() {
    local response="$1"
    local exit_code="$2"
    if [ "$GEMINI_DEBUG" = "true" ] && [ -n "$GEMINI_LOG_FILE" ]; then
        echo "" >> "$GEMINI_LOG_FILE"
        echo "================== GEMINI RESPONSE $(date +'%Y-%m-%d %H:%M:%S') ==================" >> "$GEMINI_LOG_FILE"
        echo "EXIT CODE: $exit_code" >> "$GEMINI_LOG_FILE"
        echo "RESPONSE LENGTH: $(echo "$response" | wc -c) characters" >> "$GEMINI_LOG_FILE"
        echo "RESPONSE:" >> "$GEMINI_LOG_FILE"
        echo "$response" >> "$GEMINI_LOG_FILE"
        echo "===================================================================" >> "$GEMINI_LOG_FILE"
    fi
}

# Initialize
init_gemini_wrapper() {
    mkdir -p "$GEMINI_CACHE_DIR"
    mkdir -p "$GEMINI_LOG_DIR"
    
    # Test if Gemini is available
    if ! command -v gemini >/dev/null 2>&1; then
        error "Gemini CLI not found in PATH"
        return 1
    fi
    
    log "Gemini wrapper initialized"
    debug "Cache directory: $GEMINI_CACHE_DIR"
    debug "Using model: $GEMINI_MODEL"
    gemini_debug "Debug logging enabled, log file: $GEMINI_LOG_FILE"
    return 0
}

# Implement rate limiting
enforce_rate_limit() {
    if [ -f "$RATE_LIMIT_FILE" ]; then
        local last_call=$(cat "$RATE_LIMIT_FILE" 2>/dev/null || echo "0")
        local current_time=$(date +%s)
        local time_diff=$((current_time - last_call))
        
        if [ "$time_diff" -lt "$GEMINI_RATE_LIMIT" ]; then
            local sleep_time=$((GEMINI_RATE_LIMIT - time_diff))
            log "Rate limiting: sleeping ${sleep_time}s"
            sleep "$sleep_time"
        fi
    fi
    
    # Save current time
    date +%s > "$RATE_LIMIT_FILE"
}

# Generate cache key from file content
generate_cache_key() {
    local file="$1"
    
    if [ -f "$file" ]; then
        # Create hash from file content and prompt
        echo "$SELECTED_PROMPT" | cat - "$file" | sha256sum | cut -d' ' -f1
    else
        echo ""
    fi
}

# Check if cache entry is still valid
is_cache_valid() {
    local cache_file="$1"
    
    if [ ! -f "$cache_file" ]; then
        return 1
    fi
    
    local cache_age=$(( $(date +%s) - $(stat -c %Y "$cache_file" 2>/dev/null || stat -f %m "$cache_file" 2>/dev/null) ))
    
    if [ "$cache_age" -lt "$GEMINI_CACHE_TTL" ]; then
        log "Cache hit: age ${cache_age}s (TTL: ${GEMINI_CACHE_TTL}s)"
        return 0
    else
        log "Cache expired: age ${cache_age}s"
        return 1
    fi
}

# Main function: Analyze diff file with Gemini
analyze_diff() {
    local diff_file="$1"
    
    if [ ! -f "$diff_file" ]; then
        error "Diff file not found: $diff_file"
        return 1
    fi
    
    # Check file size (max 1MB for review content with files)
    local file_size=$(stat -c%s "$diff_file" 2>/dev/null || stat -f%z "$diff_file" 2>/dev/null || echo "0")
    if [ "$file_size" -gt 1048576 ]; then
        error "Review content too large: ${file_size} bytes (max 1MB)"
        echo "The merge request is too large to analyze automatically. Please break it into smaller changes."
        return 1
    fi
    
    # Generate cache key
    local cache_key=$(generate_cache_key "$diff_file")
    if [ -z "$cache_key" ]; then
        error "Failed to generate cache key"
        return 1
    fi
    
    local cache_file="$GEMINI_CACHE_DIR/$cache_key"
    
    # Check cache
    if is_cache_valid "$cache_file"; then
        debug "Using cached result from: $cache_file"
        cat "$cache_file"
        return 0
    fi
    
    # Rate limiting
    enforce_rate_limit
    
    # Create a temporary file with context and content
    local temp_file=$(mktemp)
    local request_content="Please review the following merge request. I'm providing both the current file contents and the diffs to help you understand the context better.

${SELECTED_PROMPT}

===== MERGE REQUEST CONTENT =====
$(cat "$diff_file")"
    
    echo "$request_content" > "$temp_file"
    
    # Log the request if debug is enabled
    log_gemini_request "$request_content"
    
    # Call Gemini with -p parameter
    log "Calling Gemini for code review"
    debug "Gemini command: gemini -m $GEMINI_MODEL -p \"[content from temp file]\""
    gemini_debug "Request content length: $(echo "$request_content" | wc -c) characters"
    gemini_debug "Temp file: $temp_file"
    
    local gemini_result=""
    local gemini_exit_code=0
    
    # Use timeout command if available
    if command -v timeout >/dev/null 2>&1; then
        gemini_result=$(cat "$temp_file" | timeout "$GEMINI_TIMEOUT" gemini -m "$GEMINI_MODEL" -p "" 2>&1)
        gemini_exit_code=$?
    else
        gemini_result=$(cat "$temp_file" | gemini -m "$GEMINI_MODEL" -p "" 2>&1)
        gemini_exit_code=$?
    fi
    
    # Log the response if debug is enabled
    log_gemini_response "$gemini_result" "$gemini_exit_code"
    
    # Clean up temp file
    rm -f "$temp_file"
    
    # Check result
    if [ "$gemini_exit_code" -eq 0 ] && [ -n "$gemini_result" ]; then
        # Cache successful response
        echo "$gemini_result" > "$cache_file"
        log "Gemini analysis completed successfully"
        gemini_debug "Response length: $(echo "$gemini_result" | wc -c) characters"
        echo "$gemini_result"
        return 0
    elif [ "$gemini_exit_code" -eq 124 ]; then
        error "Gemini call timed out after ${GEMINI_TIMEOUT}s"
        gemini_debug "Timeout occurred after ${GEMINI_TIMEOUT}s"
        echo "The analysis timed out. The changes might be too complex to analyze within the time limit."
        return 1
    else
        error "Gemini call failed (exit code: $gemini_exit_code)"
        error "Output: $gemini_result"
        gemini_debug "Gemini failed with exit code $gemini_exit_code"
        gemini_debug "Error output: $gemini_result"
        echo "Failed to analyze the merge request. Please check the logs for details."
        return 1
    fi
}

# Clean up old cache entries
cleanup_cache() {
    local max_age_hours=${1:-24}  # Default: 24 hours
    
    log "Cleaning up cache older than $max_age_hours hours"
    find "$GEMINI_CACHE_DIR" -type f -mmin +$((max_age_hours * 60)) -delete 2>/dev/null
    
    local cache_files=$(find "$GEMINI_CACHE_DIR" -type f | wc -l)
    local cache_size=$(du -sh "$GEMINI_CACHE_DIR" 2>/dev/null | cut -f1)
    
    log "Cache stats: $cache_files files, $cache_size total size"
}

# Main execution
main() {
    local diff_file="$1"
    
    if [ -z "$diff_file" ]; then
        error "Usage: $0 <diff_file>"
        exit 1
    fi
    
    # Initialize
    if ! init_gemini_wrapper; then
        exit 1
    fi
    
    # Analyze the diff
    debug "Starting analysis of diff file: $diff_file"
    if analyze_diff "$diff_file"; then
        # Cleanup old cache entries in background
        cleanup_cache 48 &
        debug "Analysis completed successfully"
        exit 0
    else
        debug "Analysis failed"
        exit 1
    fi
}

# Run main function
main "$@"