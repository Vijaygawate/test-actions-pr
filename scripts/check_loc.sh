#!/usr/bin/env bash
# Enhanced LOC checker for PRs using git diff
# Usage: ./check_loc.sh <base_ref> <head_ref> <threshold>

set -e
set -x  # Print each command as it runs

if [ "$#" -ne 3 ]; then
  echo "Usage: $0 <base_ref> <head_ref> <threshold>"
  exit 1
fi

BASE_REF="$1"
HEAD_REF="$2"
THRESHOLD="$3"

# Debug: print the SHAs and check if they exist
echo "BASE_REF: $BASE_REF"
echo "HEAD_REF: $HEAD_REF"
git cat-file -e "$BASE_REF" || { echo "Base SHA $BASE_REF not found"; exit 1; }
git cat-file -e "$HEAD_REF" || { echo "Head SHA $HEAD_REF not found"; exit 1; }

# Get per-file added/deleted lines
mapfile -t DIFF_LINES < <(git diff --numstat "$BASE_REF" "$HEAD_REF")

TOTAL_ADDED=0
TOTAL_DELETED=0
TOTAL_CODE=0
TOTAL_COMMENT=0
TOTAL_BLANK=0
declare -a FILE_REPORTS

for LINE in "${DIFF_LINES[@]}"; do
  ADDED=$(echo "$LINE" | awk '{print $1}')
  DELETED=$(echo "$LINE" | awk '{print $2}')
  FILE=$(echo "$LINE" | awk '{print $3}')
  [ "$ADDED" = "-" ] && continue  # skip binary files

  # Get added lines for this file
  ADDED_LINES=$(git diff "$BASE_REF" "$HEAD_REF" -- "$FILE" | grep '^+' | grep -v '^+++' | cut -c2-)
  CODE=0; COMMENT=0; BLANK=0
  while IFS= read -r l; do
    if [[ "$l" =~ ^[[:space:]]*$ ]]; then
      ((BLANK++)) || true
    elif [[ "$l" =~ ^[[:space:]]*# ]]; then
      ((COMMENT++)) || true
    else
      ((CODE++)) || true
    fi
  done <<< "$ADDED_LINES"

  NET=$((ADDED - DELETED))
  FILE_REPORTS+=("$(printf '%7s %9s %6s %6s %8s  | %s' "+$ADDED" "-$DELETED" "$( ((NET>=0)) && echo +$NET || echo $NET )" "$CODE" "$COMMENT" "$FILE")")
  TOTAL_ADDED=$((TOTAL_ADDED + ADDED))
  TOTAL_DELETED=$((TOTAL_DELETED + DELETED))
  TOTAL_CODE=$((TOTAL_CODE + CODE))
  TOTAL_COMMENT=$((TOTAL_COMMENT + COMMENT))
  TOTAL_BLANK=$((TOTAL_BLANK + BLANK))
done

NET_CHANGE=$((TOTAL_ADDED - TOTAL_DELETED))
SIGN="+"
[ $NET_CHANGE -lt 0 ] && SIGN=""

echo "============================================================"
echo "  PR Lines of Code Report"
echo "============================================================"
echo "  Files changed  : ${#FILE_REPORTS[@]}"
echo "  Lines added    : +$TOTAL_ADDED"
echo "  Lines deleted  : -$TOTAL_DELETED"
echo "  Net change     : $SIGN$NET_CHANGE"
echo "  ---"
echo "  Added code     : $TOTAL_CODE"
echo "  Added comments : $TOTAL_COMMENT"
echo "  Added blanks   : $TOTAL_BLANK"
echo "  Threshold      : $THRESHOLD"
echo "============================================================"
echo
printf "   Added  Deleted    Net   Code  Comment  | File\n"
echo "  ----------------------------------------------------------"
for REPORT in "${FILE_REPORTS[@]}"; do
  echo "  $REPORT"
done
echo

if [ "$TOTAL_ADDED" -le "$THRESHOLD" ]; then
  echo "PASSED: Added lines ($TOTAL_ADDED) is within threshold ($THRESHOLD)"
  exit 0
else
  echo "FAILED: Added lines ($TOTAL_ADDED) exceeds threshold ($THRESHOLD)"
  echo "  Exceeds by $((TOTAL_ADDED - THRESHOLD)) lines."
  echo "  Consider splitting this PR into smaller changes."
  exit 1
fi
 
