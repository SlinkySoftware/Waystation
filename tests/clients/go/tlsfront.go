// tlsfront: a TLS front for the plain-HTTP harness stack, so the go command (which sends credentials over HTTPS
// only, from .netrc or the GOPROXY URL) can authenticate to Waystation. Standard library only; runs in the client
// container: go run tlsfront.go <listen address> <upstream URL> <certificate file to write>.
// The certificate is self-signed for "localhost" and generated at start-up (never stored outside the container).
package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"log"
	"math/big"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"time"
)

func main() {
	if len(os.Args) != 4 {
		log.Fatal("usage: tlsfront <listen address> <upstream URL> <certificate file>")
	}
	upstream, err := url.Parse(os.Args[2])
	if err != nil {
		log.Fatal(err)
	}
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		log.Fatal(err)
	}
	template := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "localhost"},
		DNSNames:              []string{"localhost"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(24 * time.Hour),
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		log.Fatal(err)
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	if err := os.WriteFile(os.Args[3], certPEM, 0o644); err != nil {
		log.Fatal(err)
	}
	proxy := httputil.NewSingleHostReverseProxy(upstream)
	server := &http.Server{
		Addr:    os.Args[1],
		Handler: proxy,
		TLSConfig: &tls.Config{
			Certificates: []tls.Certificate{{Certificate: [][]byte{der}, PrivateKey: key}},
			MinVersion:   tls.VersionTLS12,
		},
		ReadHeaderTimeout: 30 * time.Second,
	}
	log.Fatal(server.ListenAndServeTLS("", ""))
}
