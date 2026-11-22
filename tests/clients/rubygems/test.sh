#!/bin/sh
# RubyGems through Waystation (client-ruby container: the official ruby image with gem and Bundler). gem and Bundler
# install a tiny public gem through the rubygems.org proxy (compact index; the .gem verified by Waystation against
# the info checksum), gem builds and pushes a generated gem (depending on the public one) to the hosted repository with
# an API token, gem installs it from there (the gemspec Waystation marshals), Bundler installs it with its dependency
# through the group configured as the rubygems.org mirror, gem yank removes a version and Bundler then resolves the
# older one (its cached versions file is updated with Range + Repr-Digest). Wrong and missing credentials are refused.
set -eu
. /harness/lib.sh

W=$WAYSTATION_URL
HOST=${W#*://}
NAME=${HOST%%:*}  # Bundler looks credentials up by the host name (bundle config set <host> user:token)
work=/tmp/rubygems-harness
rm -rf "$work"
mkdir -p "$work"
PUBLIC=hola
PUBLIC_VERSION=0.1.3
DEMO=ws_harness_demo
export GEM_SPEC_CACHE="$work/spec-cache"

url() {  # url <repository> [<token>]: the repository URL, with credentials when a token is given
  if [ -n "${2:-}" ]; then
    printf 'http://admin:%s@%s/repository/%s/' "$2" "$HOST" "$1"
  else
    printf '%s/repository/%s/' "$W" "$1"
  fi
}

gem_install() {  # gem_install <home> <source URL> <gem args...>: a fresh gem install from one source only
  _home=$1 _source=$2
  shift 2
  HOME="$_home" GEM_HOME="$_home/gems" gem install --clear-sources --source "$_source" --no-document "$@" \
    > "$_home.log" 2>&1 || { tail -n 30 "$_home.log"; return 1; }
}

bundle_install() {  # bundle_install <project dir>: Bundler with the project's own home (cache) and config
  (cd "$1" && HOME="$1/home" BUNDLE_APP_CONFIG="$1/.bundle" BUNDLE_PATH="$1/vendor" bundle install --jobs 1 \
    > "$1/bundle.log" 2>&1) || { tail -n 30 "$1/bundle.log"; return 1; }
}

bundle_project() {  # bundle_project <dir> <source URL> <gem lines>
  mkdir -p "$1/home"
  printf "source '%s'\n\n%s\n" "$2" "$3" > "$1/Gemfile"
}

step 'API token for the repositories'
ws_login
ws_token '["read", "write", "delete"]' '["rubygems-*"]'
token=$WS_TOKEN

step "gem install through the proxy ($PUBLIC $PUBLIC_VERSION, verified against the compact index checksum)"
gem_install "$work/g-proxy" "$(url rubygems-proxy "$token")" "$PUBLIC" -v "$PUBLIC_VERSION" || fail 'gem install via proxy'
out=$(GEM_HOME="$work/g-proxy/gems" ruby -e 'require "hola"; puts Hola.hi("english")') || fail 'hola does not load'
[ "$out" = 'hello world' ] || fail "hola printed: $out"

step 'bundle install through the proxy (source in the Gemfile, credentials from bundle config)'
bundle_project "$work/b-proxy" "$(url rubygems-proxy)" "gem '$PUBLIC', '$PUBLIC_VERSION'"
(cd "$work/b-proxy" && BUNDLE_APP_CONFIG="$work/b-proxy/.bundle" bundle config set --local "$NAME" "admin:$token") \
  > /dev/null
bundle_install "$work/b-proxy" || fail 'bundle install via proxy'
grep -q "^    $PUBLIC ($PUBLIC_VERSION)" "$work/b-proxy/Gemfile.lock" || fail 'Gemfile.lock does not hold hola'

step 'a wrong token is refused'
bundle_project "$work/b-wrong" "$(url rubygems-proxy)" "gem '$PUBLIC', '$PUBLIC_VERSION'"
(cd "$work/b-wrong" && BUNDLE_APP_CONFIG="$work/b-wrong/.bundle" bundle config set --local "$NAME" 'admin:wrong') \
  > /dev/null
if bundle_install "$work/b-wrong" > /dev/null 2>&1; then
  fail 'a bundle install with a wrong token succeeded'
fi

step "gem build and gem push to the hosted repository ($DEMO 1.0.0 and 1.1.0, depending on $PUBLIC)"
for version in 1.0.0 1.1.0; do
  src="$work/src-$version"
  mkdir -p "$src/lib"
  printf 'module WsHarnessDemo\n  VERSION = "%s"\nend\n' "$version" > "$src/lib/$DEMO.rb"
  cat > "$src/$DEMO.gemspec" <<EOF
Gem::Specification.new do |s|
  s.name = '$DEMO'
  s.version = '$version'
  s.summary = 'A generated gem for the Waystation client harness'
  s.authors = ['Waystation harness']
  s.license = 'MIT'
  s.files = ['lib/$DEMO.rb']
  s.required_ruby_version = '>= 3.0'
  s.add_dependency '$PUBLIC', '~> 0.1'
end
EOF
  (cd "$src" && gem build "$DEMO.gemspec" > build.log 2>&1) || { cat "$src/build.log"; fail "gem build $version"; }
  (cd "$src" && HOME="$work" GEM_HOST_API_KEY="$token" gem push "$DEMO-$version.gem" --host "${W}/repository/rubygems-hosted" \
    > push.log 2>&1) || { cat "$src/push.log"; fail "gem push $version"; }
  grep -q "Successfully registered gem: $DEMO ($version)" "$src/push.log" || { cat "$src/push.log"; fail 'push message'; }
done
if (cd "$work/src-1.0.0" && HOME="$work" GEM_HOST_API_KEY=wrong-key gem push "$DEMO-1.0.0.gem" \
  --host "${W}/repository/rubygems-hosted" > push-wrong.log 2>&1); then
  fail 'a push with a wrong API key succeeded'
fi

step 'gem install from the hosted repository (the gemspec Waystation marshals, without dependencies)'
gem_install "$work/g-hosted" "$(url rubygems-hosted "$token")" "$DEMO" -v 1.0.0 --ignore-dependencies \
  || fail 'gem install from the hosted repository'
GEM_HOME="$work/g-hosted/gems" gem list "$DEMO" | grep -q "$DEMO (1.0.0)" || fail "$DEMO 1.0.0 is not installed"

step 'an anonymous gem install from the hosted repository is refused'
if gem_install "$work/g-anonymous" "$(url rubygems-hosted)" "$DEMO" --ignore-dependencies > /dev/null 2>&1; then
  fail 'an anonymous install from the hosted repository succeeded'
fi

step 'bundle install through the group as the rubygems.org mirror: the pushed gem and its proxied dependency'
bundle_project "$work/b-group" 'https://rubygems.org' "gem '$DEMO', '~> 1.0'"
(cd "$work/b-group" && export BUNDLE_APP_CONFIG="$work/b-group/.bundle" \
  && bundle config set --local mirror.https://rubygems.org "$(url rubygems-group)" \
  && bundle config set --local "$NAME" "admin:$token") > /dev/null
bundle_install "$work/b-group" || fail 'bundle install through the group'
grep -q "^    $DEMO (1.1.0)" "$work/b-group/Gemfile.lock" || fail '~> 1.0 did not resolve to 1.1.0'
grep -q "^    $PUBLIC ($PUBLIC_VERSION)" "$work/b-group/Gemfile.lock" || fail 'hola did not come through the group'

step "gem install through the group ($DEMO with $PUBLIC)"
gem_install "$work/g-group" "$(url rubygems-group "$token")" "$DEMO" -v 1.1.0 || fail 'gem install through the group'
GEM_HOME="$work/g-group/gems" gem list "$PUBLIC" | grep -q "$PUBLIC ($PUBLIC_VERSION)" || fail 'hola not installed'

step 'gem yank 1.1.0; Bundler then resolves ~> 1.0 to 1.0.0 (its cached index updated)'
(cd "$work" && HOME="$work" GEM_HOST_API_KEY="$token" gem yank "$DEMO" -v 1.1.0 --host "${W}/repository/rubygems-hosted" \
  > yank.log 2>&1) || { cat "$work/yank.log"; fail 'gem yank'; }
rm -rf "$work/b-group/vendor" "$work/b-group/Gemfile.lock"
bundle_install "$work/b-group" || fail 'bundle install through the group after the yank'
grep -q "^    $DEMO (1.0.0)" "$work/b-group/Gemfile.lock" || fail '~> 1.0 did not resolve to 1.0.0 after the yank'

pass
