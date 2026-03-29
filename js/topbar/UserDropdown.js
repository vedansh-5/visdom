/**
 * Copyright 2017-present, The Visdom Authors
 * All rights reserved.
 *
 * This source code is licensed under the license found in the
 * LICENSE file in the root directory of this source tree.
 *
 */

import React, { useState } from 'react';

function UserDropdown() {
  const [isOpen, setIsOpen] = useState(false);

  // TODO Hardcoded rn extend later for real auth
  const currentUser = 'Guest';

  return (
    <span>
      <span>User&nbsp;</span>
      <div className="btn-group navbar-btn" role="group" aria-label="User:">
        <div className={`btn-group ${isOpen ? 'open' : ''}`} role="group">
          <button
            className="btn btn-default dropdown-toggle"
            type="button"
            id="userDropdown"
            onClick={() => setIsOpen(!isOpen)}
            onBlur={() => setTimeout(() => setIsOpen(false), 200)}
            aria-haspopup="true"
            aria-expanded={isOpen}
          >
            {currentUser} &nbsp;
            <span className="caret" />
          </button>
          <ul className="dropdown-menu" aria-labelledby="userDropdown">
            <li>
              <a href="#guest" onClick={(e) => e.preventDefault()}>
                {currentUser}
              </a>
            </li>
            <li role="separator" className="divider" />
            <li>
              <a href="#login" onClick={(e) => e.preventDefault()}>
                Login / Register
              </a>
            </li>
          </ul>
        </div>
      </div>
    </span>
  );
}

export default UserDropdown;