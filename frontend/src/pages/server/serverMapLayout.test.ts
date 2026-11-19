import { WORLD_WIDTH } from "./serverCities";
import { mapMarkers, type CityServers } from "./serverMapLayout";

it("groups nearby cities without chaining distant endpoints, independent of input order", () => {
  const cities: CityServers[] = ["a", "b", "c", "d"].map((id, index) => ({
    city: { id, en: id, ru: id, x: 100 + index * 40, y: 100 },
    servers: [],
  }));
  const groupIds = (input: CityServers[]) =>
    mapMarkers(input, WORLD_WIDTH, 1)
      .map((marker) =>
        marker.cities
          .map(({ city }) => city.id)
          .sort()
          .join(""),
      )
      .sort();

  const groups = groupIds(cities);
  expect(groups.join("")).toBe("abcd");
  expect(groups.some((ids) => ids.length === 2)).toBe(true);
  expect(groups.every((ids) => ids.length <= 2)).toBe(true);
  expect(groupIds([...cities.slice(1), ...cities.slice(0, 1)])).toEqual(groups);
});
