// City centers (rounded WGS84 coordinates). Geographic data: GeoNames, https://www.geonames.org/ (CC BY 4.0).
// Keep identifiers stable when labels or coordinates change. The first two letters identify the country.
const cityData: ReadonlyArray<readonly [string, string, string, number, number]> = [
  ["ae-dubai", "Dubai", "Дубай", 25.205, 55.271],
  ["am-yerevan", "Yerevan", "Ереван", 40.181, 44.514],
  ["ar-buenos-aires", "Buenos Aires", "Буэнос-Айрес", -34.613, -58.378],
  ["at-vienna", "Vienna", "Вена", 48.208, 16.372],
  ["au-melbourne", "Melbourne", "Мельбурн", -37.814, 144.963],
  ["au-perth", "Perth", "Перт", -31.952, 115.861],
  ["au-sydney", "Sydney", "Сидней", -33.868, 151.207],
  ["be-brussels", "Brussels", "Брюссель", 50.851, 4.349],
  ["bg-sofia", "Sofia", "София", 42.698, 23.324],
  ["br-rio-de-janeiro", "Rio de Janeiro", "Рио-де-Жанейро", -22.903, -43.208],
  ["br-sao-paulo", "São Paulo", "Сан-Паулу", -23.548, -46.636],
  ["ca-montreal", "Montréal", "Монреаль", 45.509, -73.588],
  ["ca-toronto", "Toronto", "Торонто", 43.706, -79.399],
  ["ca-vancouver", "Vancouver", "Ванкувер", 49.249, -123.119],
  ["ch-geneva", "Geneva", "Женева", 46.202, 6.146],
  ["ch-zurich", "Zurich", "Цюрих", 47.367, 8.55],
  ["cl-santiago", "Santiago", "Сантьяго", -33.457, -70.648],
  ["cn-beijing", "Beijing", "Пекин", 39.907, 116.397],
  ["cn-shanghai", "Shanghai", "Шанхай", 31.222, 121.458],
  ["co-bogota", "Bogotá", "Богота", 4.609, -74.082],
  ["cy-nicosia", "Nicosia", "Никосия", 35.175, 33.364],
  ["cz-prague", "Prague", "Прага", 50.088, 14.421],
  ["de-berlin", "Berlin", "Берлин", 52.524, 13.411],
  ["de-dusseldorf", "Düsseldorf", "Дюссельдорф", 51.222, 6.777],
  ["de-falkenstein", "Falkenstein (Vogtland)", "Фалькенштайн (Фогтланд)", 50.478, 12.371],
  ["de-frankfurt", "Frankfurt am Main", "Франкфурт-на-Майне", 50.116, 8.684],
  ["de-munich", "Munich", "Мюнхен", 48.137, 11.576],
  ["de-nuremberg", "Nuremberg", "Нюрнберг", 49.454, 11.077],
  ["dk-copenhagen", "Copenhagen", "Копенгаген", 55.676, 12.565],
  ["ee-tallinn", "Tallinn", "Таллин", 59.437, 24.754],
  ["eg-cairo", "Cairo", "Каир", 30.063, 31.25],
  ["es-barcelona", "Barcelona", "Барселона", 41.389, 2.159],
  ["es-madrid", "Madrid", "Мадрид", 40.417, -3.703],
  ["fi-helsinki", "Helsinki", "Хельсинки", 60.16952, 24.93545],
  ["fi-tampere", "Tampere", "Тампере", 61.499, 23.787],
  ["fr-marseille", "Marseille", "Марсель", 43.297, 5.381],
  ["fr-paris", "Paris", "Париж", 48.853, 2.349],
  ["fr-roubaix", "Roubaix", "Рубе", 50.694, 3.175],
  ["fr-strasbourg", "Strasbourg", "Страсбург", 48.584, 7.746],
  ["gb-london", "London", "Лондон", 51.509, -0.126],
  ["gb-manchester", "Manchester", "Манчестер", 53.481, -2.238],
  ["ge-tbilisi", "Tbilisi", "Тбилиси", 41.694, 44.834],
  ["gr-athens", "Athens", "Афины", 37.984, 23.728],
  ["hk-hong-kong", "Hong Kong", "Гонконг", 22.279, 114.175],
  ["hr-zagreb", "Zagreb", "Загреб", 45.814, 15.978],
  ["hu-budapest", "Budapest", "Будапешт", 47.498, 19.04],
  ["id-jakarta", "Jakarta", "Джакарта", -6.214, 106.845],
  ["ie-dublin", "Dublin", "Дублин", 53.333, -6.249],
  ["il-tel-aviv", "Tel Aviv", "Тель-Авив", 32.08, 34.78],
  ["in-bengaluru", "Bengaluru", "Бангалор", 12.972, 77.594],
  ["in-mumbai", "Mumbai", "Мумбаи", 19.073, 72.883],
  ["in-new-delhi", "New Delhi", "Нью-Дели", 28.635, 77.225],
  ["is-reykjavik", "Reykjavík", "Рейкьявик", 64.136, -21.896],
  ["it-milan", "Milan", "Милан", 45.464, 9.19],
  ["it-rome", "Rome", "Рим", 41.892, 12.511],
  ["jp-osaka", "Osaka", "Осака", 34.694, 135.502],
  ["jp-tokyo", "Tokyo", "Токио", 35.689, 139.692],
  ["ke-nairobi", "Nairobi", "Найроби", -1.283, 36.817],
  ["kr-seoul", "Seoul", "Сеул", 37.566, 126.978],
  ["kz-almaty", "Almaty", "Алматы", 43.256, 76.929],
  ["kz-astana", "Astana", "Астана", 51.181, 71.446],
  ["lt-vilnius", "Vilnius", "Вильнюс", 54.689, 25.279],
  ["lu-luxembourg", "Luxembourg", "Люксембург", 49.612, 6.13],
  ["lv-riga", "Riga", "Рига", 56.946, 24.105],
  ["md-chisinau", "Chișinău", "Кишинёв", 47.005, 28.858],
  ["mx-mexico-city", "Mexico City", "Мехико", 19.428, -99.128],
  ["my-kuala-lumpur", "Kuala Lumpur", "Куала-Лумпур", 3.141, 101.687],
  ["ng-lagos", "Lagos", "Лагос", 6.455, 3.394],
  ["nl-amsterdam", "Amsterdam", "Амстердам", 52.374, 4.89],
  ["nl-dronten", "Dronten", "Дронтен", 52.525, 5.71806],
  ["nl-rotterdam", "Rotterdam", "Роттердам", 51.922, 4.479],
  ["no-oslo", "Oslo", "Осло", 59.913, 10.746],
  ["nz-auckland", "Auckland", "Окленд", -36.849, 174.763],
  ["ph-manila", "Manila", "Манила", 14.605, 120.982],
  ["pl-warsaw", "Warsaw", "Варшава", 52.23, 21.012],
  ["pt-lisbon", "Lisbon", "Лиссабон", 38.716, -9.133],
  ["ro-bucharest", "Bucharest", "Бухарест", 44.432, 26.106],
  ["rs-belgrade", "Belgrade", "Белград", 44.804, 20.465],
  ["ru-kazan", "Kazan", "Казань", 55.789, 49.122],
  ["ru-moscow", "Moscow", "Москва", 55.752, 37.616],
  ["ru-novosibirsk", "Novosibirsk", "Новосибирск", 55.042, 82.935],
  ["ru-saint-petersburg", "Saint Petersburg", "Санкт-Петербург", 59.939, 30.314],
  ["ru-vladivostok", "Vladivostok", "Владивосток", 43.105, 131.874],
  ["ru-yekaterinburg", "Yekaterinburg", "Екатеринбург", 56.852, 60.612],
  ["sa-riyadh", "Riyadh", "Эр-Рияд", 24.687, 46.721],
  ["se-stockholm", "Stockholm", "Стокгольм", 59.32938, 18.06871],
  ["sg-singapore", "Singapore", "Сингапур", 1.3521, 103.8198],
  ["sk-bratislava", "Bratislava", "Братислава", 48.148, 17.106],
  ["th-bangkok", "Bangkok", "Бангкок", 13.754, 100.501],
  ["tr-ankara", "Ankara", "Анкара", 39.92, 32.854],
  ["tr-istanbul", "Istanbul", "Стамбул", 41.014, 28.95],
  ["tw-taipei", "Taipei", "Тайбэй", 25.048, 121.532],
  ["ua-kyiv", "Kyiv", "Киев", 50.454, 30.523],
  ["us-ashburn", "Ashburn", "Ашберн", 39.043, -77.487],
  ["us-atlanta", "Atlanta", "Атланта", 33.749, -84.388],
  ["us-boston", "Boston", "Бостон", 42.358, -71.06],
  ["us-chicago", "Chicago", "Чикаго", 41.85, -87.65],
  ["us-dallas", "Dallas", "Даллас", 32.783, -96.807],
  ["us-denver", "Denver", "Денвер", 39.739, -104.985],
  ["us-fremont", "Fremont", "Фримонт", 37.5483, -121.9886],
  ["us-los-angeles", "Los Angeles", "Лос-Анджелес", 34.052, -118.244],
  ["us-miami", "Miami", "Майами", 25.774, -80.194],
  ["us-new-york", "New York", "Нью-Йорк", 40.714, -74.006],
  ["us-phoenix", "Phoenix", "Финикс", 33.448, -112.074],
  ["us-san-francisco", "San Francisco", "Сан-Франциско", 37.775, -122.419],
  ["us-san-jose", "San Jose", "Сан-Хосе", 37.339, -121.895],
  ["us-seattle", "Seattle", "Сиэтл", 47.606, -122.332],
  ["uz-tashkent", "Tashkent", "Ташкент", 41.265, 69.217],
  ["vn-ho-chi-minh-city", "Ho Chi Minh City", "Хошимин", 10.823, 106.63],
  ["za-cape-town", "Cape Town", "Кейптаун", -33.926, 18.424],
  ["za-johannesburg", "Johannesburg", "Йоханнесбург", -26.202, 28.044],
];

export const WORLD_WIDTH = 800;
export const WORLD_HEIGHT = 335;

export type Point = { x: number; y: number };
export type ServerCity = Point & { id: string; en: string; ru: string };

/** Natural Earth 1 polynomial and the viewBox transform used in mapWorld.json. */
function project(latitude: number, longitude: number): Point {
  const radians = Math.PI / 180;
  const phi = latitude * radians;
  const lambda = longitude * radians;
  const scale = 132.2348685362038;
  const x =
    lambda *
    (0.8707 - 0.131979 * phi ** 2 - 0.013791 * phi ** 4 + 0.003971 * phi ** 10 - 0.001529 * phi ** 12);
  const y =
    phi * (1.007226 + 0.015085 * phi ** 2 - 0.044475 * phi ** 6 + 0.028874 * phi ** 8 - 0.005916 * phi ** 10);
  return { x: 400 + scale * x, y: 194.3172821611262 - scale * y };
}

export const serverCities = new Map<string, ServerCity>(
  cityData.map(([id, en, ru, latitude, longitude]) => [id, { id, en, ru, ...project(latitude, longitude) }]),
);

export function cityName(city: ServerCity, language: string): string {
  return language.startsWith("ru") ? city.ru : city.en;
}
