// 商品目录（真实项目里是接口返回的数据）
const catalog = {
  updated: "2026-11-17",
  items: [
    { name: "苹果", price: 5 },
    { name: "香蕉", price: 3 },
    { name: "橙子", price: 4 },
  ],
};

function render(products) {
  const list = document.getElementById("list");
  for (const p of products) {
    const li = document.createElement("li");
    li.textContent = `${p.name} ¥${p.price}`;
    list.appendChild(li);
  }
  document.getElementById("count").textContent = `共 ${products.length} 件`;
}

render(catalog.products);
