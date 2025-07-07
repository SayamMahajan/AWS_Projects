const express = require('express');
const app = express();
const port = process.env.PORT || 3000;

app.get('/', (req, res) => {
  res.send('Hello from DevOps Dashboard App! This is for demo CI/CD pipeline.');
});

app.listen(port, () => {
  console.log(`App is running on http://localhost:${port}`);
});
