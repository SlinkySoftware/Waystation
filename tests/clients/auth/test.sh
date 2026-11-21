#!/bin/sh
# Client-auth matrix (T-A04): one script per client container (HARNESS_CLIENT), in the order of CLIENT in format.env.
set -eu
exec sh "/harness/auth/$HARNESS_CLIENT.sh"
