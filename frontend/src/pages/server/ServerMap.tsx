import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { useTranslation } from "react-i18next";

import type { Server } from "../../api/client";
import { SIGNAL_TONE, StatusPill } from "../../components/StatusPill";
import { splitFlag } from "../../lib/flags";
import { serverSignal } from "../../lib/serverSignal";
import world from "./mapWorld.json";
import { cityName, WORLD_HEIGHT, WORLD_WIDTH, type Point } from "./serverCities";
import {
  cityCenter,
  clampCenter,
  mapCities,
  MAP_MAX_WIDTH,
  mapMarkers,
  signalSectors,
  WORLD_CENTER,
  type CityServers,
  type MapMarker,
} from "./serverMapLayout";
import styles from "./ServerMap.module.css";

const MOBILE = "(max-width: 767px)";
const ZOOM_LEVELS = [1, 2, 4, 8, 16] as const;
const ARROW_DELTA: Record<string, Point> = {
  ArrowLeft: { x: -1, y: 0 },
  ArrowRight: { x: 1, y: 0 },
  ArrowUp: { x: 0, y: -1 },
  ArrowDown: { x: 0, y: 1 },
};

type Selection = { selectedCities: ReadonlySet<string>; onSelect: (ids: ReadonlySet<string>) => void };

export function ServerMap({ servers, selectedCities, onSelect }: Selection & { servers: Server[] }) {
  const { t, i18n } = useTranslation();
  const headingId = useId();
  const panelId = useId();
  const [mobile, setMobile] = useState(() => window.matchMedia(MOBILE).matches);
  const [override, setOverride] = useState<boolean>();
  const open = override ?? !mobile;
  const cities = mapCities(servers);
  const mapped = cities.reduce((count, entry) => count + entry.servers.length, 0);
  const number = new Intl.NumberFormat(i18n.resolvedLanguage);

  useEffect(() => {
    const media = window.matchMedia(MOBILE);
    const update = () => setMobile(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  function toggle() {
    setOverride(!open);
    if (open) onSelect(new Set());
  }

  return (
    <section className={styles.card} aria-labelledby={headingId}>
      <h2 className={styles.heading} id={headingId}>
        <button
          type="button"
          className={styles.toggle}
          onClick={toggle}
          aria-expanded={open}
          aria-controls={panelId}
        >
          <span className={styles.headingText}>
            {t("servers.map.title")}
            <small className={styles.mapped}>
              {t("servers.map.mapped", {
                mapped: number.format(mapped),
                total: number.format(servers.length),
              })}
            </small>
          </span>
          <span className={styles.chevron} aria-hidden="true" />
        </button>
      </h2>
      <div id={panelId} hidden={!open}>
        {open && <MapPanel cities={cities} selectedCities={selectedCities} onSelect={onSelect} />}
      </div>
    </section>
  );
}

function MapPanel({ cities, selectedCities, onSelect }: Selection & { cities: CityServers[] }) {
  const { t, i18n } = useTranslation();
  const plot = useRef<HTMLDivElement>(null);
  const drag = useRef<{ id: number; clientX: number; clientY: number; center: Point; unit: number } | null>(
    null,
  );
  const [dragging, setDragging] = useState(false);
  const [width, setWidth] = useState(MAP_MAX_WIDTH);
  const [level, setLevel] = useState(0);
  const [center, setCenter] = useState(WORLD_CENTER);
  const zoom = ZOOM_LEVELS[level] ?? 1;
  const markers = useMemo(() => mapMarkers(cities, width, zoom), [cities, width, zoom]);
  const mappedServers = cities.flatMap(({ servers }) => servers);
  const selection = cities.filter(({ city }) => selectedCities.has(city.id));
  const selectedCity = selection.length === 1 ? selection[0]?.city : undefined;
  const language = i18n.resolvedLanguage ?? "en";

  useEffect(() => {
    const element = plot.current;
    if (!element) return;
    const measure = () => {
      const next = element.getBoundingClientRect().width;
      if (next > 0) setWidth(next);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  function changeZoom(step: number) {
    const next = ZOOM_LEVELS[level + step];
    if (next === undefined) return;
    setCenter(clampCenter(selection.length > 0 ? cityCenter(selection) : center, next));
    setLevel(level + step);
  }

  function panWithKeyboard(event: KeyboardEvent<HTMLDivElement>) {
    const direction = ARROW_DELTA[event.key];
    if (!direction || zoom === 1 || event.target !== event.currentTarget) return;
    event.preventDefault();
    setCenter((previous) =>
      clampCenter(
        {
          x: previous.x + (direction.x * WORLD_WIDTH) / (5 * zoom),
          y: previous.y + (direction.y * WORLD_HEIGHT) / (5 * zoom),
        },
        zoom,
      ),
    );
  }

  function startDrag(event: PointerEvent<HTMLDivElement>) {
    if (zoom === 1 || event.button !== 0 || !event.isPrimary || (event.target as Element).closest("button"))
      return;
    drag.current = {
      id: event.pointerId,
      clientX: event.clientX,
      clientY: event.clientY,
      center,
      unit: WORLD_WIDTH / (event.currentTarget.getBoundingClientRect().width * zoom),
    };
    event.currentTarget.setPointerCapture(event.pointerId);
    setDragging(true);
  }

  function moveDrag(event: PointerEvent<HTMLDivElement>) {
    const origin = drag.current;
    if (!origin || origin.id !== event.pointerId) return;
    setCenter(
      clampCenter(
        {
          x: origin.center.x - (event.clientX - origin.clientX) * origin.unit,
          y: origin.center.y - (event.clientY - origin.clientY) * origin.unit,
        },
        zoom,
      ),
    );
  }

  function endDrag(event: PointerEvent<HTMLDivElement>) {
    if (drag.current?.id !== event.pointerId) return;
    drag.current = null;
    setDragging(false);
    if (event.type !== "lostpointercapture" && event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }

  return (
    <>
      <div className={styles.zoom} role="group" aria-label={t("servers.map.zoom")}>
        <button
          type="button"
          onClick={() => changeZoom(-1)}
          aria-label={t("servers.map.zoomOut")}
          aria-disabled={level === 0}
        >
          −
        </button>
        <output className={styles.zoomLevel}>{zoom}×</output>
        <button
          type="button"
          onClick={() => changeZoom(1)}
          aria-label={t("servers.map.zoomIn")}
          aria-disabled={level === ZOOM_LEVELS.length - 1}
        >
          +
        </button>
      </div>
      <div
        ref={plot}
        className={styles.plot}
        data-zoomed={zoom > 1}
        data-dragging={dragging}
        role="group"
        aria-label={t("servers.map.world")}
        tabIndex={zoom > 1 ? 0 : undefined}
        onKeyDown={panWithKeyboard}
        onPointerDown={startDrag}
        onPointerMove={moveDrag}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onLostPointerCapture={endDrag}
      >
        <svg className={styles.world} viewBox={`0 0 ${WORLD_WIDTH} ${WORLD_HEIGHT}`} aria-hidden="true">
          <g
            transform={`translate(${WORLD_CENTER.x} ${WORLD_CENTER.y}) scale(${zoom}) translate(${-center.x} ${-center.y})`}
          >
            <path className={styles.grid} d={world.grid} />
            <path className={styles.land} d={world.land} />
          </g>
        </svg>
        {markers.map((marker) => {
          const x = 50 + ((marker.x - center.x) * zoom * 100) / WORLD_WIDTH;
          const y = 50 + ((marker.y - center.y) * zoom * 100) / WORLD_HEIGHT;
          if (x < 0 || x > 100 || y < 0 || y > 100) return null;
          const pressed =
            marker.cities.length === selection.length &&
            marker.cities.every(({ city }) => selectedCities.has(city.id));
          return (
            <MarkerButton
              key={marker.key}
              marker={marker}
              x={x}
              y={y}
              pressed={pressed}
              onClick={() => onSelect(new Set(pressed ? [] : marker.cities.map(({ city }) => city.id)))}
            />
          );
        })}
      </div>
      {mappedServers.length > 0 && (
        <ul className={styles.legend} aria-label={t("servers.map.legend")}>
          {signalSectors(mappedServers).map(({ signal }) => (
            <li key={signal} data-signal={signal}>
              {t(`servers.map.signals.${signal}`)}
            </li>
          ))}
        </ul>
      )}
      <div className={styles.details} aria-live="polite">
        {selection.length > 0 ? (
          <div role="group" aria-label={t("servers.map.selected")}>
            <strong>{selectedCity ? cityName(selectedCity, language) : t("servers.map.group")}</strong>
            <ul>
              {selection
                .flatMap(({ servers }) => servers)
                .map((server) => {
                  const { signal, label } = serverSignal(server);
                  return (
                    <li key={server.id}>
                      {splitFlag(server.name).rest || server.name}
                      <StatusPill tone={SIGNAL_TONE[signal]}>{label}</StatusPill>
                    </li>
                  );
                })}
            </ul>
          </div>
        ) : (
          <p>{t(mappedServers.length ? "servers.map.hint" : "servers.map.empty")}</p>
        )}
      </div>
    </>
  );
}

function MarkerButton({
  marker,
  x,
  y,
  pressed,
  onClick,
}: {
  marker: MapMarker;
  x: number;
  y: number;
  pressed: boolean;
  onClick: () => void;
}) {
  const { t, i18n } = useTranslation();
  const isCity = marker.cities.length === 1;
  const name = isCity
    ? cityName(marker.cities[0].city, i18n.resolvedLanguage ?? "en")
    : t("servers.map.group");
  const servers = marker.cities.flatMap(({ servers }) => servers);
  const count = servers.length;
  const sectors = signalSectors(servers);
  const number = new Intl.NumberFormat(i18n.resolvedLanguage);
  const [first] = servers;
  const status =
    count === 1 && first
      ? serverSignal(first).label
      : sectors
          .map(
            ({ signal, count: members }) => `${t(`servers.map.signals.${signal}`)} ${number.format(members)}`,
          )
          .join(", ");
  return (
    <button
      type="button"
      className={styles.marker}
      style={{ left: `${x}%`, top: `${y}%` }}
      data-label-side={x > 76 ? "left" : "right"}
      data-group={count > 1}
      data-signal={sectors[0]?.signal}
      aria-label={`${name}, ${t("servers.fleet.total", { count })}, ${status}`}
      aria-pressed={pressed}
      onClick={onClick}
    >
      {count === 1 ? (
        <span className={styles.dot} aria-hidden="true" />
      ) : (
        <span className={styles.ring} aria-hidden="true">
          <svg viewBox="0 0 36 36">
            {sectors.map(({ signal, length, offset }) => (
              <circle
                key={signal}
                data-signal={signal}
                cx="18"
                cy="18"
                r="14"
                pathLength="100"
                strokeDasharray={`${length} ${100 - length}`}
                strokeDashoffset={-offset}
              />
            ))}
          </svg>
          <span>{number.format(count)}</span>
        </span>
      )}
      <span className={styles.markerLabel} aria-hidden="true">
        {name}
      </span>
    </button>
  );
}
