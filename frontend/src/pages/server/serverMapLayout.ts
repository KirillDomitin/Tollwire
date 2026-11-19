import type { Server } from "../../api/client";
import { serverSignal, type Signal } from "../../lib/serverSignal";
import { serverCities, WORLD_HEIGHT, WORLD_WIDTH, type Point, type ServerCity } from "./serverCities";

export const MAP_MAX_WIDTH = 620;
const MARKER_DISTANCE = 46;
const SIGNALS: readonly Signal[] = ["ok", "warn", "down", "process", "idle"];

export type CityServers = { city: ServerCity; servers: Server[] };
type CityGroup = [CityServers, ...CityServers[]];
export type MapMarker = Point & { key: string; cities: CityGroup };

export function mapCities(servers: Server[]): CityServers[] {
  const groups = new Map<string, CityServers>();
  for (const server of servers) {
    const city = serverCities.get(server.location_city_id ?? "");
    if (!city) continue;
    const existing = groups.get(city.id);
    if (existing) existing.servers.push(server);
    else groups.set(city.id, { city, servers: [server] });
  }
  return [...groups.values()];
}

export function cityCenter(cities: CityServers[]): Point {
  const sum = cities.reduce(({ x, y }, { city }) => ({ x: x + city.x, y: y + city.y }), { x: 0, y: 0 });
  return { x: sum.x / cities.length, y: sum.y / cities.length };
}

function maxDistanceSquared(a: CityGroup, b: CityGroup): number {
  let max = 0;
  for (const { city: first } of a) {
    for (const { city: second } of b) {
      max = Math.max(max, (first.x - second.x) ** 2 + (first.y - second.y) ** 2);
    }
  }
  return max;
}

/** Merge the closest groups while every pair of cities in a marker stays within the threshold. */
export function mapMarkers(cities: CityServers[], width: number, zoom: number): MapMarker[] {
  const limit = ((MARKER_DISTANCE * WORLD_WIDTH) / (width * zoom)) ** 2;
  const groups = [...cities]
    .sort((a, b) => a.city.id.localeCompare(b.city.id))
    .map<CityGroup>((city) => [city]);
  while (groups.length > 1) {
    let closest = Infinity;
    let pair: [CityGroup, CityGroup] | undefined;
    for (const [index, first] of groups.entries()) {
      for (const second of groups.slice(index + 1)) {
        const distance = maxDistanceSquared(first, second);
        if (distance <= limit && distance < closest) {
          closest = distance;
          pair = [first, second];
        }
      }
    }
    if (!pair) break;
    const [first, second] = pair;
    first.push(...second);
    groups.splice(groups.indexOf(second), 1);
  }
  return groups.map((group) => ({
    ...cityCenter(group),
    key: group.map(({ city }) => city.id).join("/"),
    cities: group,
  }));
}

export function signalSectors(servers: Server[]) {
  const counts = new Map<Signal, number>();
  for (const server of servers) {
    const { signal } = serverSignal(server);
    counts.set(signal, (counts.get(signal) ?? 0) + 1);
  }
  let offset = 0;
  return SIGNALS.flatMap((signal) => {
    const count = counts.get(signal);
    if (!count) return [];
    const length = (count / servers.length) * 100;
    const sector = { signal, count, length, offset };
    offset += length;
    return [sector];
  });
}

export const WORLD_CENTER: Point = { x: WORLD_WIDTH / 2, y: WORLD_HEIGHT / 2 };

export function clampCenter(center: Point, zoom: number): Point {
  const marginX = WORLD_CENTER.x / zoom;
  const marginY = WORLD_CENTER.y / zoom;
  return {
    x: Math.max(marginX, Math.min(WORLD_WIDTH - marginX, center.x)),
    y: Math.max(marginY, Math.min(WORLD_HEIGHT - marginY, center.y)),
  };
}
