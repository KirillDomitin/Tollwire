import i18n from "../../i18n";
import type { BotProtocol } from "./useBot";

/** Протокол для выбора: «US2 · AmneziaWG». Названия видов - те же, что в журнале аудита. */
export function protocolLabel(protocol: BotProtocol): string {
  const path = `audit.values.protocol_type.${protocol.type}`;
  const kind = i18n.exists(path) ? i18n.t(path as "audit.values.protocol_type.amnezia_wg") : protocol.type;
  return `${protocol.server_name} · ${kind}`;
}
