/* Classic 5V Nano + HLK-M30 + common-cathode traffic light.
 * R -> D4, Y -> D5, G -> D6, GND -> common GND.
 * The photographed light board already has current-limiting resistors.
 * M30 TX -> D10; D11 -> 10k -> M30 RX, RX -> 20k -> GND.
 * M30 uses a separate regulated 3.3V supply. Its UART must be 9600.
 * USB Serial Monitor: 115200, CR / LF / CRLF.
 * USB or TCP: LIGHT RED|YELLOW|GREEN|OFF|AUTO|STATUS|HELP + newline.
 * AI: LIGHT AI IDLE|RUNNING|WAITING|ERROR|OFFLINE; 15s heartbeat lease.
 * One command at a time; wait for its reply before sending the next.
 * Startup: lights OFF; after 5 seconds verify HLK-M30, then send TS=1.
 * USB at+ commands are buffered and forwarded with CR, without case changes.
 * In transparent mode, return M30 to AT using LAN TS=0 or module power-cycle.
 */
#include <SoftwareSerial.h>
#include <ctype.h>
#include <string.h>

// D5 keeps the Arduino hardware PWM frequency; Timer0/millis are unchanged.

SoftwareSerial module(10, 11); // RX, TX
const uint8_t RED_PIN = 4, YELLOW_PIN = 5, GREEN_PIN = 6;
const uint8_t OFF = 0, RED = 1, YELLOW = 2, GREEN = 3;
const uint8_t WAIT_BOOT = 0, WAIT_VERSION = 1, SEND_MODE = 2,
              WAIT_MODE = 3, START_DONE = 4;
uint8_t color = OFF, networkStart = WAIT_BOOT;
bool automatic = false;
const uint8_t AI_OFFLINE = 0, AI_IDLE = 1, AI_RUNNING = 2, AI_WAITING = 3, AI_ERROR = 4;
bool aiActive = false;
const uint8_t SOLID = 0, BREATHE = 1, SLOW = 2, FAST = 3;
uint8_t aiState = AI_OFFLINE, aiLevel = 0, aiColor = OFF, aiEffect = SOLID;
bool aiCustom = false;
unsigned long aiStarted = 0, aiLastHeartbeat = 0, aiLastFrame = 0;
const unsigned long AI_LEASE_MS = 15000UL;
const unsigned long AI_FRAME_MS = 4UL, AI_BREATH_MS = 2400UL;
unsigned long phaseStarted = 0, networkStarted = 0;

struct LineBuffer {
  char data[80];
  uint8_t length;
  bool discard;
  unsigned long lastByte;
};
LineBuffer usbLine = {}, wifiLine = {};
void handleLine(char *line, bool fromUsb);

void setColor(uint8_t next) {
  // Break before make, including transitions out of manual mode.
  digitalWrite(RED_PIN, LOW);
  digitalWrite(YELLOW_PIN, LOW);
  digitalWrite(GREEN_PIN, LOW);
  if (next == RED) digitalWrite(RED_PIN, HIGH);
  if (next == YELLOW) digitalWrite(YELLOW_PIN, HIGH);
  if (next == GREEN) digitalWrite(GREEN_PIN, HIGH);
  color = next;
  aiLevel = next == OFF ? 0 : 255; // Keep duty bookkeeping valid after manual control.
  phaseStarted = millis();
}

void printAiState(Print &out) {
  out.print(F("OK AI state="));
  switch (aiState) {
    case AI_IDLE: out.print(F("IDLE")); break;
    case AI_RUNNING: out.print(F("RUNNING")); break;
    case AI_WAITING: out.print(F("WAITING")); break;
    case AI_ERROR: out.print(F("ERROR")); break;
    default: out.print(F("OFFLINE")); break;
  }
  // These are commanded outputs, not an ADC measurement. digitalRead(D5)
  // would disable PWM on AVR, so never read the PWM pin for this reply.
  out.print(F(" R=")); out.print(color == RED ? 1 : 0);
  out.print(F(" Y=")); out.print(color == YELLOW ? 1 : 0);
  out.print(F(" G=")); out.print(color == GREEN ? 1 : 0);
  out.print(F(" level=")); out.print(aiLevel);
  out.print(F(" lease=")); out.print(AI_LEASE_MS);
  if (aiCustom) {
    out.print(F(" color="));
    switch (aiColor) {
      case RED: out.print(F("RED")); break;
      case YELLOW: out.print(F("YELLOW")); break;
      case GREEN: out.print(F("GREEN")); break;
      default: out.print(F("OFF")); break;
    }
    out.print(F(" effect="));
    switch (aiEffect) {
      case BREATHE: out.print(F("BREATHE")); break;
      case SLOW: out.print(F("SLOW")); break;
      case FAST: out.print(F("FAST")); break;
      default: out.print(F("SOLID")); break;
    }
  }
  out.println();
}

// Smoothstep has zero slope at both ends. Squaring approximates perceptual
// brightness correction; fixed-point math avoids floating point on the Nano.
// Keep the 20..255 duty range compatible with the existing workbench protocol.
uint8_t breathingLevel(unsigned long elapsed) {
  unsigned long ramp = elapsed % AI_BREATH_MS;
  if (ramp > AI_BREATH_MS / 2) ramp = AI_BREATH_MS - ramp;
  uint32_t x = ramp * 1024UL / (AI_BREATH_MS / 2);
  uint32_t eased = (x * x * (3072UL - 2UL * x) + 524288UL) >> 20;
  return 20 + ((235UL * eased * eased + 524288UL) >> 20);
}

void applyAiOutput(uint8_t next, uint8_t level) {
  if (next != color) {
    // Turn off only the previous channel before enabling a different one.
    // Never digitalWrite(D5) between breathing frames: it disconnects PWM.
    if (color == RED) digitalWrite(RED_PIN, LOW);
    else if (color == YELLOW) digitalWrite(YELLOW_PIN, LOW);
    else if (color == GREEN) digitalWrite(GREEN_PIN, LOW);
    if (next == RED) digitalWrite(RED_PIN, HIGH);

  }
  if ((next == YELLOW || next == GREEN) && (next != color || level != aiLevel))
    analogWrite(next == YELLOW ? YELLOW_PIN : GREEN_PIN, level);
  color = next;
  aiLevel = level;
}

void updateAiPattern() {
  if (!aiActive) return;
  unsigned long now = millis();
  if (aiState != AI_OFFLINE && now - aiLastHeartbeat >= AI_LEASE_MS) {
    aiState = AI_OFFLINE; aiColor = OFF; aiEffect = SOLID;
    aiStarted = now;
  }
  if (now - aiLastFrame < AI_FRAME_MS) return;
  aiLastFrame = now;
  unsigned long elapsed = now - aiStarted;
  uint8_t next = OFF, level = 0;
  if (aiState != AI_OFFLINE && aiColor != OFF) {
    next = aiColor;
    if (aiEffect == BREATHE) level = breathingLevel(elapsed);
    else if (aiEffect == SLOW) level = elapsed % 1000UL < 500UL ? 255 : 0;
    else if (aiEffect == FAST) level = elapsed % 400UL < 200UL ? 255 : 0;
    else level = 255;
    if (!level) next = OFF;
  }
  if (next != color || level != aiLevel) {
    applyAiOutput(next, level);
  }
}

void setAiState(uint8_t next, uint8_t nextColor, uint8_t effect, bool custom) {
  unsigned long now = millis();
  if (!aiActive || aiState != next || aiColor != nextColor || aiEffect != effect) {
    aiStarted = now;
    aiLastFrame = now - AI_FRAME_MS;
  }
  aiActive = true; automatic = false; aiState = next;
  aiColor = nextColor; aiEffect = effect; aiCustom = custom;
  aiLastHeartbeat = now;
  updateAiPattern();
}

void printState(Print &out) {
  if (aiActive) { printAiState(out); return; }
  out.print(F("OK LIGHT mode="));
  out.print(automatic ? F("AUTO") : F("MANUAL"));
  out.print(F(" color="));
  switch (color) {
    case RED: out.print(F("RED")); break;
    case YELLOW: out.print(F("YELLOW")); break;
    case GREEN: out.print(F("GREEN")); break;
    default: out.print(F("OFF")); break;
  }
  out.print(F(" R=")); out.print(digitalRead(RED_PIN));
  out.print(F(" Y=")); out.print(digitalRead(YELLOW_PIN));
  out.print(F(" G=")); out.println(digitalRead(GREEN_PIN));
}

void replyState(bool fromUsb) {
  if (!fromUsb) printState(module);
  printState(Serial);
}

void replyError(bool fromUsb, const __FlashStringHelper *message) {
  if (!fromUsb) module.println(message);
  Serial.println(message);
}

bool isLightPrefix(const char *line) {
  return strncmp(line, "LIGHT ", 6) == 0 || strcmp(line, "LIGHT") == 0;
}

void handleLine(char *line, bool fromUsb) {
  while (*line == ' ' || *line == '\t') ++line;
  size_t n = strlen(line);
  while (n && (line[n-1] == ' ' || line[n-1] == '\t')) line[--n] = 0;
  if (!n) return;

  if (!fromUsb) {
    // Only framed LIGHT commands execute. AT echoes and responses never reply
    // back to the module, avoiding an echo/error feedback loop in AT mode.
    if (networkStart == WAIT_VERSION && strncmp(line, "HLK-M30(", 8) == 0)
      networkStart = SEND_MODE;
    if (networkStart == WAIT_MODE &&
        (strcmp(line, "0:ok") == 0 || strcmp(line, "ok") == 0 ||
         strcmp(line, "OK") == 0)) {
      networkStart = START_DONE;
      Serial.println(F("NET READY: TCP server, use LIGHT commands."));
    }
    Serial.print(F("[M30] ")); Serial.println(line);
  }

  // Preserve the spelling and case of SSIDs, passwords and other AT values.
  if (fromUsb && n >= 3 && tolower((unsigned char)line[0]) == 'a' &&
      tolower((unsigned char)line[1]) == 't' && line[2] == '+') {
    networkStart = START_DONE;
    module.print(line);
    module.write('\r');
    Serial.println(F("AT SENT (module must be in AT mode)."));
    return;
  }
  for (char *p = line; *p; ++p) *p = toupper((unsigned char)*p);
  if (fromUsb && strcmp(line, "NET ON") == 0) {
    networkStart = WAIT_VERSION;
    networkStarted = millis();
    module.print(F("at+ver=?\r"));
    Serial.println(F("Checking module AT mode..."));
    return;
  }
  if (!isLightPrefix(line)) {
    if (fromUsb) Serial.println(F("ERR use LIGHT HELP, NET ON, or an at+ command."));
    return;
  }
  if (!fromUsb) networkStart = START_DONE; // Network control is already active.
  char *argument = line + 5;
  while (*argument == ' ' || *argument == '\t') ++argument;
  if (strcmp(argument, "HELP") == 0) {
    const __FlashStringHelper *help = F("HELP LIGHT RED|YELLOW|GREEN|OFF|AUTO|STATUS; AI IDLE|RUNNING|WAITING|ERROR|OFFLINE");
    if (!fromUsb) module.println(help);
    Serial.println(help);
    return;
  }
  if (strcmp(argument, "STATUS") == 0) {
    replyState(fromUsb);
    return;
  }
  if (strncmp(argument, "AI ", 3) == 0) {
    char *rest;
    char *state = strtok_r(argument + 3, " \t", &rest);
    char *colorArg = strtok_r(NULL, " \t", &rest);
    char *effectArg = strtok_r(NULL, " \t", &rest);
    if (!state || (colorArg && !effectArg) || strtok_r(NULL, " \t", &rest)) {
      replyError(fromUsb, F("ERR LIGHT expected AI STATE [COLOR EFFECT]")); return;
    }
    uint8_t nextAi;
    if (strcmp(state, "IDLE") == 0) nextAi = AI_IDLE;
    else if (strcmp(state, "RUNNING") == 0) nextAi = AI_RUNNING;
    else if (strcmp(state, "WAITING") == 0) nextAi = AI_WAITING;
    else if (strcmp(state, "ERROR") == 0) nextAi = AI_ERROR;
    else if (strcmp(state, "OFFLINE") == 0) nextAi = AI_OFFLINE;
    else { replyError(fromUsb, F("ERR LIGHT unknown AI state")); return; }
    uint8_t nextColor = nextAi == AI_IDLE ? GREEN : nextAi == AI_ERROR ? RED :
                        nextAi == AI_OFFLINE ? OFF : YELLOW;
    uint8_t effect = nextAi == AI_RUNNING ? BREATHE : nextAi == AI_WAITING ? SLOW :
                     nextAi == AI_ERROR ? FAST : SOLID;
    if (colorArg) {
      if (strcmp(colorArg, "RED") == 0) nextColor = RED;
      else if (strcmp(colorArg, "YELLOW") == 0) nextColor = YELLOW;
      else if (strcmp(colorArg, "GREEN") == 0) nextColor = GREEN;
      else if (strcmp(colorArg, "OFF") == 0) nextColor = OFF;
      else { replyError(fromUsb, F("ERR LIGHT unknown color")); return; }
      if (strcmp(effectArg, "SOLID") == 0) effect = SOLID;
      else if (strcmp(effectArg, "BREATHE") == 0) effect = BREATHE;
      else if (strcmp(effectArg, "SLOW") == 0) effect = SLOW;
      else if (strcmp(effectArg, "FAST") == 0) effect = FAST;
      else { replyError(fromUsb, F("ERR LIGHT unknown effect")); return; }
      if ((effect == BREATHE && nextColor != YELLOW && nextColor != GREEN) ||
          (nextColor == OFF && effect != SOLID) || (nextAi == AI_OFFLINE && nextColor != OFF)) {
        replyError(fromUsb, F("ERR LIGHT unsupported color/effect")); return;
      }
    }
    setAiState(nextAi, nextColor, effect, colorArg != NULL);
    replyState(fromUsb);
    return;
  }
  uint8_t next;
  if (strcmp(argument, "RED") == 0) next = RED;
  else if (strcmp(argument, "YELLOW") == 0) next = YELLOW;
  else if (strcmp(argument, "GREEN") == 0) next = GREEN;
  else if (strcmp(argument, "OFF") == 0) next = OFF;
  else if (strcmp(argument, "AUTO") == 0) {
    aiActive = false;
    automatic = true;
    setColor(RED);
    replyState(fromUsb);
    return;
  } else {
    replyError(fromUsb, F("ERR LIGHT unknown command; use LIGHT HELP"));
    return;
  }
  aiActive = false;
  automatic = false;
  setColor(next);
  replyState(fromUsb);
}

void readLines(Stream &input, LineBuffer &buffer, bool fromUsb) {
  // Bound each turn so timers and the other serial input keep progressing.
  uint8_t budget = 48;
  while (budget-- && input.available()) {
    char c = input.read();
    buffer.lastByte = millis();
    if (c == '\r' || c == '\n') {
      if (buffer.discard) {
        // An oversized or invalid frame must not execute its tail as a command.
        if (fromUsb) Serial.println(F("ERR LIGHT invalid/overlong line (max 79 bytes)"));
        else if (buffer.length >= 6 && strncmp(buffer.data, "LIGHT ", 6) == 0)
          replyError(false, F("ERR LIGHT invalid/overlong line (max 79 bytes)"));
      } else if (buffer.length) {
        buffer.data[buffer.length] = 0;
        handleLine(buffer.data, fromUsb);
      }
      buffer.length = 0;
      buffer.discard = false;
    } else if (!buffer.discard) {
      if ((c < 32 && c != '\t') || c > 126 || buffer.length >= sizeof(buffer.data)-1)
        buffer.discard = true;
      else buffer.data[buffer.length++] = c;
    }
  }
  // Keep discarding until a delimiter after a stalled partial frame.
  if (buffer.length && millis() - buffer.lastByte > 2000UL) buffer.discard = true;
}

void updateCycle() {
  if (!automatic) return;
  unsigned long duration = color == YELLOW ? 2000UL : 5000UL;
  if (millis() - phaseStarted >= duration)
    setColor(color == RED ? GREEN : color == GREEN ? YELLOW : RED);
}

void updateNetworkStart() {
  if (networkStart == WAIT_BOOT && millis() >= 5000UL) {
    networkStarted = millis();
    networkStart = WAIT_VERSION;
    module.print(F("at+ver=?\r"));
  } else if (networkStart == SEND_MODE) {
    networkStarted = millis();
    networkStart = WAIT_MODE;
    module.print(F("at+TS=1\r"));
  } else if ((networkStart == WAIT_VERSION || networkStart == WAIT_MODE) &&
             millis() - networkStarted > 3000UL) {
    networkStart = START_DONE;
    Serial.println(F("NET unconfirmed: module may already be transparent; LIGHT input remains enabled."));
  }
}

void setup() {
  pinMode(RED_PIN, OUTPUT);
  pinMode(YELLOW_PIN, OUTPUT);
  pinMode(GREEN_PIN, OUTPUT);
  setColor(OFF);
  Serial.begin(115200);
  module.begin(9600);
  Serial.println(F("Nano traffic light v2.2 configurable AI; USB=115200 M30=9600; R=D4 Y=D5 G=D6"));
  Serial.println(F("Startup OFF. Wi-Fi setup in 5s. Type LIGHT HELP."));
}

void loop() {
  readLines(module, wifiLine, false);
  readLines(Serial, usbLine, true);
  updateCycle();
  updateAiPattern();
  updateNetworkStart();
}
