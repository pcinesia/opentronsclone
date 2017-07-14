import React from 'react';
import { Switch, Route } from 'react-router';
import App from './containers/App';
import MainPage from './containers/MainPage';
import AboutPage from './containers/AboutPage';

export default () => (
  <App>
    <Switch>
      <Route path="/" component={MainPage} />
      <Route path="/welcome" component={MainPage} />
      <Route path="/about" component={AboutPage} />
    </Switch>
  </App>
);
