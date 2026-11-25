#include <ESP8266WiFi.h>
#include <DNSServer.h>

// The Mac running server.py should use the fixed address 192.168.4.254.
const char* WIFI_NAME = "Baikal_Conference";
const char* WIFI_PASSWORD = "Baikal2026";
const byte DNS_PORT = 53;

IPAddress apAddress(192, 168, 4, 1);
IPAddress subnet(255, 255, 255, 0);
IPAddress portalHost(192, 168, 4, 254);
DNSServer dnsServer;

void setup() {
  Serial.begin(115200);
  delay(200);
  WiFi.mode(WIFI_AP);
  WiFi.softAPConfig(apAddress, apAddress, subnet);
  bool started = WiFi.softAP(WIFI_NAME, WIFI_PASSWORD, 6, false, 8);

  if (!started) {
    Serial.println("ERROR: Wi-Fi access point failed to start");
    return;
  }

  // Resolve every hostname to the local Mac portal. This lets captive-network
  // assistants and browser requests reach the offline site over HTTP.
  dnsServer.setErrorReplyCode(DNSReplyCode::NoError);
  dnsServer.start(DNS_PORT, "*", portalHost);

  Serial.println();
  Serial.println("Baikal Conference Wi-Fi is ready");
  Serial.print("SSID: "); Serial.println(WIFI_NAME);
  Serial.print("Password: "); Serial.println(WIFI_PASSWORD);
  Serial.print("AP address: "); Serial.println(WiFi.softAPIP());
  Serial.print("Portal host: http://"); Serial.println(portalHost);
}

void loop() {
  dnsServer.processNextRequest();
  delay(2);
}
