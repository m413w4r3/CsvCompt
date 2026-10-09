import { createBrowserRouter } from 'react-router-dom'
import { AppShell } from '../components/layout/AppShell'
import { Overview } from '../pages/Overview/index'
import { Expenses } from '../pages/Expenses/index'
import { Income } from '../pages/Income/index'
import { Family } from '../pages/Family/index'
import { Merchants } from '../pages/Merchants/index'
import { Budgets } from '../pages/Budgets/index'
import { Review } from '../pages/Review/index'
import { Insights } from '../pages/Insights/index'
import { Transactions } from '../pages/Transactions/index'
import { Settings } from '../pages/Settings/index'
import { NotFound } from '../pages/NotFound/index'

export const router = createBrowserRouter([
  { path: '/', element: <AppShell />, children: [
    { index: true, element: <Overview /> },
    { path: 'expenses', element: <Expenses /> }, { path: 'income', element: <Income /> },
    { path: 'family', element: <Family /> }, { path: 'merchants', element: <Merchants /> },
    { path: 'budgets', element: <Budgets /> }, { path: 'review', element: <Review /> },
    { path: 'insights', element: <Insights /> }, { path: 'transactions', element: <Transactions /> },
    { path: 'settings', element: <Settings /> }, { path: '*', element: <NotFound /> },
  ] },
])
